import hashlib
import json
import queue
import re
import shlex
import tempfile
import threading
import time
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

from loguru import logger
from pydantic import Field
from pydantic import model_validator
from pygtail import Pygtail

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import resolve_agent_reference
from imbue.mngr.api.find import resolve_host_reference
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.data_types import VolumeFileType
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.utils.cel_utils import apply_cel_filters_to_context

FOLLOW_POLL_INTERVAL_SECONDS: Final[float] = 1.0
SOURCE_SCAN_INTERVAL_SECONDS: Final[float] = 10.0
ONLINE_CHECK_INTERVAL_SECONDS: Final[float] = 30.0
_EVENTS_JSONL_FILENAME: Final[str] = "events.jsonl"
_ROTATED_FILE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^events\.jsonl\.(\d+)$")


# =============================================================================
# Data types
# =============================================================================


class EventsTarget(FrozenModel):
    """Resolved target for the events command."""

    volume: Volume | None = Field(default=None, description="Volume scoped to the target's events directory")
    online_host: OnlineHostInterface | None = Field(
        default=None, description="Online host for direct command execution"
    )
    events_path: Path | None = Field(default=None, description="Absolute path to the events directory on the host")
    display_name: str = Field(description="Human-readable name for the target (agent or host)")
    provider: BaseProviderInstance | None = Field(
        default=None, description="Provider instance for re-checking online status"
    )
    host_id: HostId | None = Field(default=None, description="Host ID for re-checking online status")
    events_subpath: Path | None = Field(
        default=None, description="Events subpath relative to host_dir for refreshing the target"
    )

    model_config = {"arbitrary_types_allowed": True}

    @model_validator(mode="after")
    def _validate_online_host_and_events_path_are_paired(self) -> "EventsTarget":
        """Ensure online_host and events_path are either both set or both None."""
        is_host_set = self.online_host is not None
        is_path_set = self.events_path is not None
        if is_host_set != is_path_set:
            raise MngrError("online_host and events_path must both be set or both be None")
        return self


class EventRecord(FrozenModel):
    """A single parsed event from a JSONL event file."""

    raw_line: str = Field(description="The JSONL line (may be re-serialized if source was corrected)")
    timestamp: str = Field(description="ISO 8601 timestamp from the event envelope")
    event_id: str = Field(description="Unique event ID from the envelope")
    source: str = Field(description="Event source (subdirectory name)")
    data: dict[str, Any] = Field(description="Full parsed JSON dict for CEL filtering")
    original_source: str | None = Field(
        default=None,
        description="Original source from the event JSON if it differed from the path-derived source",
    )


class EventSourceInfo(FrozenModel):
    """Describes a discovered event source (a subdirectory containing events.jsonl)."""

    source_path: str = Field(description="Path relative to events dir, e.g. 'messages' or 'logs/mngr'")
    rotated_files: tuple[str, ...] = Field(
        description="Sorted rotated file names, oldest first (e.g. events.jsonl.3, events.jsonl.2, events.jsonl.1)"
    )
    is_current_file_present: bool = Field(default=True, description="Whether events.jsonl exists in this source")


class _AllEventsStreamState(MutableModel):
    """Mutable state for the all-events streaming loop."""

    emitted_event_ids: set[str] = Field(
        default_factory=set, description="Event IDs already emitted, for deduplication"
    )
    known_source_paths: set[str] = Field(
        default_factory=set, description="Source paths for which tail threads have been started"
    )
    known_rotated_files: dict[str, set[str]] = Field(
        default_factory=dict, description="Map from source_path to set of rotated file names already read"
    )
    is_online: bool = Field(default=False, description="Whether the target is currently considered online")
    last_source_scan_time: float = Field(default=0.0, description="Monotonic time of last directory scan")
    warned_incorrect_sources: set[str] = Field(
        default_factory=set,
        description="Original source values for which a mismatch warning has already been emitted",
    )


def resolve_events_target(
    identifier: str,
    mngr_ctx: MngrContext,
) -> EventsTarget:
    """Resolve a target identifier (agent or host name/ID) to an EventsTarget.

    First tries to find an agent with the given identifier.
    If no agent is found, tries to find a host.
    Uses resolve_agent_reference and resolve_host_reference from api/find.py.

    When the target host is online, the returned EventsTarget includes the
    online host and events path for direct command execution (e.g., tail -f).
    """
    with log_span("Loading agents and hosts"):
        agents_by_host, _providers = discover_hosts_and_agents(
            mngr_ctx,
            provider_names=None,
            agent_identifiers=(identifier,),
            include_destroyed=False,
            reset_caches=False,
        )

    all_hosts = list(agents_by_host.keys())

    # Try finding as an agent first
    # Only suppress "not found" errors; re-raise ambiguity ("Multiple") errors
    try:
        agent_result = resolve_agent_reference(identifier, None, agents_by_host)
    except UserInputError as e:
        if "Multiple" in str(e):
            raise
        logger.trace("Agent lookup did not find {}: {}", identifier, e)
        agent_result = None

    if agent_result is not None:
        host_ref, agent_ref = agent_result
        with log_span("Getting events access for agent {}", agent_ref.agent_name):
            provider = get_provider_instance(host_ref.provider_name, mngr_ctx)

            # Try to get the volume
            host_volume = provider.get_volume_for_host(host_ref.host_id)
            events_volume: Volume | None = None
            if host_volume is not None:
                agent_volume = host_volume.get_agent_volume(agent_ref.agent_id)
                events_volume = agent_volume.scoped("events")

            # Try to get the online host for direct access
            agent_events_subpath = Path("agents") / str(agent_ref.agent_id) / "events"
            online_host, events_path = _try_get_online_host_for_events(
                provider, host_ref.host_id, agent_events_subpath
            )

            if events_volume is None and online_host is None:
                raise MngrError(
                    f"Provider '{host_ref.provider_name}' does not support volumes and the host is not online. "
                    "Cannot read events for this agent."
                )

        return EventsTarget(
            volume=events_volume,
            online_host=online_host,
            events_path=events_path,
            display_name=f"agent '{agent_ref.agent_name}'",
            provider=provider,
            host_id=host_ref.host_id,
            events_subpath=agent_events_subpath,
        )

    # Try finding as a host
    # Only suppress "not found" errors; re-raise ambiguity ("Multiple") errors
    try:
        host_ref = resolve_host_reference(identifier, all_hosts)
    except UserInputError as e:
        if "Multiple" in str(e):
            raise
        logger.trace("Host lookup did not find {}: {}", identifier, e)
        host_ref = None

    if host_ref is not None:
        with log_span("Getting events access for host {}", host_ref.host_name):
            provider = get_provider_instance(host_ref.provider_name, mngr_ctx)

            # Try to get the volume
            host_volume = provider.get_volume_for_host(host_ref.host_id)
            events_volume = None
            if host_volume is not None:
                events_volume = host_volume.volume.scoped("events")

            # Try to get the online host for direct access
            host_events_subpath = Path("events")
            online_host, events_path = _try_get_online_host_for_events(provider, host_ref.host_id, host_events_subpath)

            if events_volume is None and online_host is None:
                raise MngrError(
                    f"Provider '{host_ref.provider_name}' does not support volumes and the host is not online. "
                    "Cannot read events for this host."
                )

        return EventsTarget(
            volume=events_volume,
            online_host=online_host,
            events_path=events_path,
            display_name=f"host '{host_ref.host_name}'",
            provider=provider,
            host_id=host_ref.host_id,
            events_subpath=host_events_subpath,
        )

    raise UserInputError(f"No agent or host found with name or ID: {identifier}")


def _try_get_online_host_for_events(
    provider: BaseProviderInstance,
    host_id: HostId,
    events_subpath: Path,
) -> tuple[OnlineHostInterface | None, Path | None]:
    """Try to get the online host and compute the absolute events path.

    Returns (online_host, events_path) if the host is online, (None, None) otherwise.
    """
    try:
        host_interface = provider.get_host(host_id)
    except MngrError as e:
        logger.trace("Host {} is not available for direct event access: {}", host_id, e)
        return None, None

    if not isinstance(host_interface, OnlineHostInterface):
        return None, None

    events_path = host_interface.host_dir / str(events_subpath)
    return host_interface, events_path


@pure
def _extract_filename(path: str) -> str:
    """Extract the filename from a volume path."""
    return path.rsplit("/", 1)[-1] if "/" in path else path


# =============================================================================
# Read event content
# =============================================================================


def read_event_content(target: EventsTarget, event_file_name: str) -> str:
    """Read the full content of an event file."""
    # Prefer host-based reading (direct access to the online host)
    if target.online_host is not None and target.events_path is not None:
        return _read_event_content_via_host(
            target.online_host, target.events_path, event_file_name, target.display_name
        )

    # Fall back to volume-based reading
    if target.volume is not None:
        with log_span("Reading event file '{}' for {} via volume", event_file_name, target.display_name):
            content_bytes = target.volume.read_file(event_file_name)
            return content_bytes.decode("utf-8", errors="replace")

    raise MngrError(f"Cannot read event file for {target.display_name}: no volume or online host available")


def _read_event_content_via_host(
    online_host: OnlineHostInterface,
    events_path: Path,
    event_file_name: str,
    display_name: str,
) -> str:
    """Read event content by executing cat on the online host."""
    with log_span("Reading event file '{}' for {} via host", event_file_name, display_name):
        file_path = events_path / event_file_name
        result = online_host.execute_idempotent_command(
            f"cat {shlex.quote(str(file_path))}",
            timeout_seconds=30.0,
        )
        if not result.success:
            raise MngrError(f"Failed to read event file '{event_file_name}': {result.stderr}")
        return result.stdout


# =============================================================================
# Source filtering
# =============================================================================


@pure
def filter_sources_by_name(
    sources: list[EventSourceInfo],
    source_filters: Sequence[str],
) -> list[EventSourceInfo]:
    """Filter event sources to only those matching the given source names.

    If source_filters is empty, returns all sources unchanged.
    """
    if not source_filters:
        return sources
    allowed = set(source_filters)
    return [s for s in sources if s.source_path in allowed]


# =============================================================================
# Event parsing and sorting
# =============================================================================


@pure
def parse_event_line(line: str, source_hint: str) -> EventRecord | None:
    """Parse a single JSONL line into an EventRecord.

    Returns None if the line cannot be parsed (malformed JSON, missing required fields).
    Always uses source_hint (derived from the file path) as the authoritative source;
    if the event JSON contains a different source, it is corrected and the mismatch
    is recorded in the returned EventRecord's original_source field.
    Generates a deterministic fallback event_id from the line hash if missing.
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.trace("Skipped malformed JSONL line: {}", stripped[:100])
        return None
    if not isinstance(data, dict):
        return None

    timestamp = data.get("timestamp", "")
    if not timestamp:
        return None

    event_id = data.get("event_id", "")
    if not event_id:
        # Generate deterministic fallback from line content
        event_id = "hash-" + hashlib.sha256(stripped.encode()).hexdigest()[:24]

    # The source_hint (derived from the file path) is always authoritative.
    # If the event JSON contains a different source, we correct it and record
    # the mismatch so a warning can be emitted downstream.
    event_source = data.get("source", "")
    original_source: str | None = None
    if event_source and event_source != source_hint:
        original_source = event_source
        logger.trace(
            "Correcting event source from '{}' to '{}' for event {}",
            event_source,
            source_hint,
            event_id,
        )

    source = source_hint
    data["source"] = source

    # Re-serialize raw_line if the source was corrected so downstream
    # consumers (e.g. CLI output) see the correct source
    corrected_raw_line = json.dumps(data, separators=(",", ":")) if original_source is not None else stripped

    return EventRecord(
        raw_line=corrected_raw_line,
        timestamp=timestamp,
        event_id=event_id,
        source=source,
        data=data,
        original_source=original_source,
    )


def _create_source_mismatch_warning(original_source: str, correct_source: str) -> EventRecord:
    """Create a warning event about an incorrect source field encountered in the stream."""
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond * 1000:09d}Z"
    warning_event_id = f"evt-{uuid4().hex}"
    message = (
        f"Event source field mismatch: event had source='{original_source}' "
        f"but was found under path '{correct_source}'. "
        f"The source has been corrected to '{correct_source}'."
    )
    data: dict[str, Any] = {
        "timestamp": timestamp,
        "type": "warn_about_incorrect_source_field",
        "event_id": warning_event_id,
        "source": "event_watcher",
        "original_source": original_source,
        "correct_source": correct_source,
        "message": message,
    }
    return EventRecord(
        raw_line=json.dumps(data, separators=(",", ":")),
        timestamp=timestamp,
        event_id=warning_event_id,
        source="event_watcher",
        data=data,
    )


def _maybe_emit_source_mismatch_warning(
    event: EventRecord,
    warned_sources: set[str],
    on_event: Callable[[EventRecord], None],
) -> None:
    """If the event had an incorrect source, emit a warning event (at most once per source)."""
    if event.original_source is not None and event.original_source not in warned_sources:
        warned_sources.add(event.original_source)
        warning = _create_source_mismatch_warning(event.original_source, event.source)
        on_event(warning)


@pure
def sort_events_by_timestamp(events: Sequence[EventRecord]) -> list[EventRecord]:
    """Sort events by their timestamp field (lexicographic on ISO 8601 works correctly)."""
    return sorted(events, key=lambda e: e.timestamp)


def _event_passes_cel_filters(
    event: EventRecord,
    cel_include_filters: Sequence[Any],
    cel_exclude_filters: Sequence[Any],
) -> bool:
    """Check whether an event passes the given CEL include/exclude filters."""
    if not cel_include_filters and not cel_exclude_filters:
        return True
    return apply_cel_filters_to_context(
        event.data,
        cel_include_filters,
        cel_exclude_filters,
        error_context_description=f"event {event.event_id}",
    )


@pure
def _sort_rotated_files_oldest_first(filenames: Sequence[str]) -> list[str]:
    """Sort rotated file names so oldest (highest number) comes first.

    Input: ['events.jsonl.1', 'events.jsonl.3', 'events.jsonl.2']
    Output: ['events.jsonl.3', 'events.jsonl.2', 'events.jsonl.1']
    """
    numbered: list[tuple[int, str]] = []
    for name in filenames:
        match = _ROTATED_FILE_PATTERN.match(name)
        if match:
            numbered.append((int(match.group(1)), name))
    # Sort by number descending (highest number = oldest)
    numbered.sort(key=lambda pair: pair[0], reverse=True)
    return [name for _, name in numbered]


# =============================================================================
# Event source discovery
# =============================================================================


def discover_event_sources(target: EventsTarget) -> list[EventSourceInfo]:
    """Find all event sources (subdirectories containing events.jsonl files)."""
    if target.online_host is not None and target.events_path is not None:
        return _discover_event_sources_via_host(target.online_host, target.events_path)

    if target.volume is not None:
        return _discover_event_sources_via_volume(target.volume)

    raise MngrError(f"Cannot discover event sources for {target.display_name}: no volume or online host available")


def _discover_event_sources_via_host(
    online_host: OnlineHostInterface,
    events_path: Path,
) -> list[EventSourceInfo]:
    """Find all events.jsonl files recursively under events_path via host commands."""
    with log_span("Discovering event sources via host"):
        cmd = f"find {shlex.quote(str(events_path))} -name 'events.jsonl*' -type f 2>/dev/null | sort; true"
        result = online_host.execute_idempotent_command(cmd, timeout_seconds=15.0)
        if not result.stdout.strip():
            return []

        return _parse_discovered_files(result.stdout, str(events_path))


@pure
def _build_event_sources_from_grouped_files(
    files_by_dir: dict[str, list[str]],
) -> list[EventSourceInfo]:
    """Build EventSourceInfo objects from files grouped by directory."""
    sources: list[EventSourceInfo] = []
    for dir_path, filenames in sorted(files_by_dir.items()):
        rotated = [f for f in filenames if _ROTATED_FILE_PATTERN.match(f)]
        is_current_present = _EVENTS_JSONL_FILENAME in filenames
        sources.append(
            EventSourceInfo(
                source_path=dir_path,
                rotated_files=tuple(_sort_rotated_files_oldest_first(rotated)),
                is_current_file_present=is_current_present,
            )
        )
    return sources


@pure
def _parse_discovered_files(find_output: str, events_path_str: str) -> list[EventSourceInfo]:
    """Parse find command output into EventSourceInfo objects.

    Groups files by their parent directory (relative to events_path) and identifies
    rotated files vs the current events.jsonl.
    """
    # Normalize the base path for stripping
    base = events_path_str.rstrip("/") + "/"

    # Group files by their parent directory relative to events_path
    files_by_dir: dict[str, list[str]] = {}
    for line in find_output.strip().split("\n"):
        file_path = line.strip()
        if not file_path:
            continue

        # Strip the base path to get the relative path
        if file_path.startswith(base):
            relative = file_path[len(base) :]
        else:
            continue

        # Split into directory and filename
        if "/" in relative:
            dir_part = relative.rsplit("/", 1)[0]
            file_part = relative.rsplit("/", 1)[1]
        else:
            dir_part = ""
            file_part = relative

        # Only include events.jsonl files (current or rotated)
        if file_part == _EVENTS_JSONL_FILENAME or _ROTATED_FILE_PATTERN.match(file_part):
            if dir_part not in files_by_dir:
                files_by_dir[dir_part] = []
            files_by_dir[dir_part].append(file_part)

    return _build_event_sources_from_grouped_files(files_by_dir)


def _discover_event_sources_via_volume(volume: Volume) -> list[EventSourceInfo]:
    """Find all events.jsonl files recursively in the volume."""
    with log_span("Discovering event sources via volume"):
        all_files = _recursive_listdir_via_volume(volume, "")
        return _group_volume_files_into_sources(all_files)


def _recursive_listdir_via_volume(volume: Volume, path: str) -> list[tuple[str, str]]:
    """Recursively list all files under a volume path.

    Returns list of (dir_path, filename) tuples.
    """
    result: list[tuple[str, str]] = []
    try:
        entries = volume.listdir(path)
    except (MngrError, OSError) as e:
        logger.trace("Failed to list volume directory '{}': {}", path, e)
        return result

    for entry in entries:
        if entry.file_type == VolumeFileType.FILE:
            filename = _extract_filename(entry.path)
            # Only include events.jsonl files
            if filename == _EVENTS_JSONL_FILENAME or _ROTATED_FILE_PATTERN.match(filename):
                result.append((path, filename))
        elif entry.file_type == VolumeFileType.DIRECTORY:
            child_path = entry.path if entry.path else path
            result.extend(_recursive_listdir_via_volume(volume, child_path))
        else:
            pass

    return result


@pure
def _group_volume_files_into_sources(files: Sequence[tuple[str, str]]) -> list[EventSourceInfo]:
    """Group (dir_path, filename) tuples into EventSourceInfo objects."""
    files_by_dir: dict[str, list[str]] = {}
    for dir_path, filename in files:
        if dir_path not in files_by_dir:
            files_by_dir[dir_path] = []
        files_by_dir[dir_path].append(filename)

    return _build_event_sources_from_grouped_files(files_by_dir)


# =============================================================================
# Reading events from sources
# =============================================================================


def _read_events_from_file(
    target: EventsTarget,
    # Path to the file relative to the events directory (e.g. "messages/events.jsonl")
    relative_file_path: str,
    source_hint: str,
) -> tuple[list[EventRecord], int]:
    """Read and parse all events from a single JSONL file.

    Returns (events, byte_length) where byte_length is the size of the raw content.
    """
    try:
        content = read_event_content(target, relative_file_path)
    except (MngrError, OSError) as e:
        logger.trace("Failed to read event file '{}': {}", relative_file_path, e)
        return [], 0

    events: list[EventRecord] = []
    for line in content.split("\n"):
        record = parse_event_line(line, source_hint)
        if record is not None:
            events.append(record)

    return events, len(content.encode("utf-8"))


def read_all_historical_events(
    target: EventsTarget,
    sources: Sequence[EventSourceInfo],
    cel_include_filters: Sequence[Any],
    cel_exclude_filters: Sequence[Any],
) -> tuple[list[EventRecord], dict[str, int]]:
    """Read all events from all sources (rotated files and current files).

    Returns (sorted_events, byte_offsets) where byte_offsets maps source_path to the
    byte length of the current events.jsonl (for subsequent tailing).
    """
    all_events: list[EventRecord] = []
    byte_offsets: dict[str, int] = {}

    for source in sources:
        source_hint = source.source_path

        # Read rotated files (oldest first)
        for rotated_file in source.rotated_files:
            relative_path = f"{source.source_path}/{rotated_file}" if source.source_path else rotated_file
            events, _ = _read_events_from_file(target, relative_path, source_hint)
            all_events.extend(events)

        # Read current file
        if source.is_current_file_present:
            relative_path = (
                f"{source.source_path}/{_EVENTS_JSONL_FILENAME}" if source.source_path else _EVENTS_JSONL_FILENAME
            )
            events, byte_length = _read_events_from_file(target, relative_path, source_hint)
            all_events.extend(events)
            byte_offsets[source.source_path] = byte_length
        else:
            byte_offsets[source.source_path] = 0

    # Sort by timestamp
    sorted_events = sort_events_by_timestamp(all_events)

    # Apply CEL filters
    sorted_events = [
        e for e in sorted_events if _event_passes_cel_filters(e, cel_include_filters, cel_exclude_filters)
    ]

    return sorted_events, byte_offsets


# =============================================================================
# Streaming all events
# =============================================================================


def _collect_historical_events(
    target: EventsTarget,
    state: _AllEventsStreamState,
    cel_include_filters: Sequence[Any],
    cel_exclude_filters: Sequence[Any],
    source_filters: Sequence[str],
) -> tuple[list[EventRecord], list[EventSourceInfo], dict[str, int]]:
    """Discover sources and read all historical/archived events (Phases 1 and 3)."""
    with log_span("Reading historical events for {}", target.display_name):
        sources = filter_sources_by_name(discover_event_sources(target), source_filters)
        all_events, initial_byte_offsets = read_all_historical_events(
            target, sources, cel_include_filters, cel_exclude_filters
        )
        for source in sources:
            state.known_source_paths.add(source.source_path)
            state.known_rotated_files[source.source_path] = set(source.rotated_files)

    return all_events, sources, initial_byte_offsets


def _start_tail_threads_for_sources(
    target: EventsTarget,
    sources: Sequence[EventSourceInfo],
    initial_byte_offsets: dict[str, int],
    event_queue: queue.Queue[EventRecord],
    cel_include_filters: Sequence[Any],
    cel_exclude_filters: Sequence[Any],
    stop_event: threading.Event,
    offset_dir_path: Path,
) -> list[threading.Thread]:
    """Start per-source tail threads for all current events.jsonl files."""
    threads: list[threading.Thread] = []
    for source in sources:
        if source.is_current_file_present:
            thread = _start_tail_thread(
                target=target,
                source_path=source.source_path,
                event_queue=event_queue,
                cel_include_filters=cel_include_filters,
                cel_exclude_filters=cel_exclude_filters,
                stop_event=stop_event,
                offset_dir_path=offset_dir_path,
                initial_byte_offset=initial_byte_offsets.get(source.source_path, 0),
            )
            threads.append(thread)
    return threads


def _emit_historical_events(
    all_events: list[EventRecord],
    state: _AllEventsStreamState,
    on_event: Callable[[EventRecord], None],
    head_count: int | None,
    tail_count: int | None,
) -> None:
    """Apply head/tail truncation and emit historical events, deduplicating by event_id."""
    if head_count is not None:
        all_events = all_events[:head_count]
    elif tail_count is not None:
        all_events = all_events[-tail_count:]
    else:
        pass

    for event in all_events:
        if event.event_id in state.emitted_event_ids:
            continue
        state.emitted_event_ids.add(event.event_id)
        _maybe_emit_source_mismatch_warning(event, state.warned_incorrect_sources, on_event)
        on_event(event)


def stream_all_events(
    target: EventsTarget,
    on_event: Callable[[EventRecord], None],
    cel_include_filters: Sequence[Any],
    cel_exclude_filters: Sequence[Any],
    tail_count: int | None,
    head_count: int | None,
    is_follow: bool,
    source_filters: Sequence[str] = (),
) -> None:
    """Stream all events from all sources."""
    state = _AllEventsStreamState(
        is_online=target.online_host is not None,
        last_source_scan_time=time.monotonic(),
    )
    stop_event = threading.Event()
    event_queue: queue.Queue[EventRecord] = queue.Queue()
    tail_threads: list[threading.Thread] = []
    offset_dir: tempfile.TemporaryDirectory[str] | None = None

    try:
        # Discover sources and read all historical events
        all_events, sources, initial_byte_offsets = _collect_historical_events(
            target, state, cel_include_filters, cel_exclude_filters, source_filters
        )

        # Start tail threads for follow mode
        if is_follow:
            offset_dir = tempfile.TemporaryDirectory(prefix="mngr-events-offsets-")
            tail_threads = _start_tail_threads_for_sources(
                target,
                sources,
                initial_byte_offsets,
                event_queue,
                cel_include_filters,
                cel_exclude_filters,
                stop_event,
                Path(offset_dir.name),
            )

        # Rotation guard: re-scan for newly rotated files that appeared during startup
        with log_span("Checking for newly rotated files"):
            rotation_guard_events = _check_for_new_archived_events(
                target, state, cel_include_filters, cel_exclude_filters, source_filters
            )
            all_events.extend(rotation_guard_events)
            all_events = sort_events_by_timestamp(all_events)

        # Emit historical events
        _emit_historical_events(all_events, state, on_event, head_count, tail_count)

        if head_count is not None or not is_follow:
            return

        # Follow mode: consume events from queue
        _consume_event_queue(
            target_holder=[target],
            state=state,
            event_queue=event_queue,
            on_event=on_event,
            cel_include_filters=cel_include_filters,
            cel_exclude_filters=cel_exclude_filters,
            stop_event=stop_event,
            tail_threads=tail_threads,
            offset_dir_path=Path(offset_dir.name) if offset_dir is not None else None,
            source_filters=source_filters,
        )

    finally:
        stop_event.set()
        for thread in tail_threads:
            thread.join(timeout=5.0)
        if offset_dir is not None:
            offset_dir.cleanup()


def _check_for_new_archived_events(
    target: EventsTarget,
    state: _AllEventsStreamState,
    cel_include_filters: Sequence[Any],
    cel_exclude_filters: Sequence[Any],
    source_filters: Sequence[str] = (),
) -> list[EventRecord]:
    """Re-scan for rotated files that appeared since the initial scan.

    This handles the case where rotation happens while we are reading
    directories and starting tail threads. Any newly rotated files
    are read and their events returned.
    """
    try:
        current_sources = filter_sources_by_name(discover_event_sources(target), source_filters)
    except (MngrError, OSError) as e:
        logger.trace("Failed to re-scan for rotated files: {}", e)
        return []

    new_events: list[EventRecord] = []
    for source in current_sources:
        known_rotated = state.known_rotated_files.get(source.source_path, set())
        for rotated_file in source.rotated_files:
            if rotated_file not in known_rotated:
                logger.debug("Found new rotated file during rotation guard: {}/{}", source.source_path, rotated_file)
                relative_path = f"{source.source_path}/{rotated_file}" if source.source_path else rotated_file
                events, _ = _read_events_from_file(target, relative_path, source.source_path)
                new_events.extend(events)
                # Record that we've now read this rotated file
                if source.source_path not in state.known_rotated_files:
                    state.known_rotated_files[source.source_path] = set()
                state.known_rotated_files[source.source_path].add(rotated_file)

    # Apply CEL filters
    new_events = [e for e in new_events if _event_passes_cel_filters(e, cel_include_filters, cel_exclude_filters)]

    return new_events


def _start_tail_thread(
    target: EventsTarget,
    source_path: str,
    event_queue: queue.Queue[EventRecord],
    cel_include_filters: Sequence[Any],
    cel_exclude_filters: Sequence[Any],
    stop_event: threading.Event,
    offset_dir_path: Path,
    initial_byte_offset: int,
) -> threading.Thread:
    """Start a daemon thread that tails a single events.jsonl and pushes events to the queue."""
    is_local = target.online_host is not None and target.online_host.is_local
    if is_local and target.events_path is not None:
        # Use pygtail for local filesystem tailing
        events_file_path = target.events_path / source_path / _EVENTS_JSONL_FILENAME

        # Pre-write the pygtail offset file so it starts from exactly where
        # the historical read left off, avoiding a gap between Phase 1 and Phase 2
        _write_pygtail_offset_file(events_file_path, source_path, offset_dir_path, initial_byte_offset)

        thread = threading.Thread(
            target=_tail_source_thread_local,
            args=(
                events_file_path,
                source_path,
                event_queue,
                cel_include_filters,
                cel_exclude_filters,
                stop_event,
                offset_dir_path,
            ),
            daemon=True,
        )
    else:
        # Use polling for remote hosts
        thread = threading.Thread(
            target=_tail_source_thread_remote,
            args=(
                target,
                source_path,
                event_queue,
                cel_include_filters,
                cel_exclude_filters,
                stop_event,
                initial_byte_offset,
            ),
            daemon=True,
        )
    thread.start()
    return thread


def _pygtail_offset_file_path(source_path: str, offset_dir_path: Path) -> str:
    """Return the path to the pygtail offset file for a given source."""
    offset_file_name = source_path.replace("/", "_") if source_path else "root"
    return str(offset_dir_path / f"{offset_file_name}.offset")


def _write_pygtail_offset_file(
    events_file_path: Path,
    source_path: str,
    offset_dir_path: Path,
    byte_offset: int,
) -> None:
    """Pre-write a pygtail offset file so tailing starts from the given byte position.

    Pygtail's offset file format is: inode\\noffset\\n
    This ensures no gap between the historical read (Phase 1) and the tail (Phase 2).
    """
    offset_file = _pygtail_offset_file_path(source_path, offset_dir_path)
    try:
        inode = events_file_path.stat().st_ino
        Path(offset_file).write_text(f"{inode}\n{byte_offset}\n")
    except OSError as e:
        logger.trace("Failed to pre-write pygtail offset file for '{}': {}", source_path, e)


def _tail_source_thread_local(
    events_file_path: Path,
    source_path: str,
    event_queue: queue.Queue[EventRecord],
    cel_include_filters: Sequence[Any],
    cel_exclude_filters: Sequence[Any],
    stop_event: threading.Event,
    offset_dir_path: Path,
) -> None:
    """Thread function that tails a local events.jsonl via pygtail."""
    offset_file = _pygtail_offset_file_path(source_path, offset_dir_path)

    while not stop_event.is_set():
        try:
            # Create a new Pygtail instance each iteration. Pygtail reads from
            # offset_file on construction, so subsequent iterations pick up where
            # the previous one left off. The offset file is pre-written by
            # _write_pygtail_offset_file before the thread starts.
            tail = Pygtail(
                str(events_file_path),
                offset_file=offset_file,
                save_on_end=True,
                read_from_end=False,
                full_lines=True,
                # files can and do get rotated, and we need to handle that
                copytruncate=True,
            )
            for line in tail:
                if stop_event.is_set():
                    break
                record = parse_event_line(line, source_path)
                if record is None:
                    continue
                if not _event_passes_cel_filters(record, cel_include_filters, cel_exclude_filters):
                    continue
                event_queue.put(record)
        except (OSError, IOError) as e:
            logger.trace("Pygtail error for source '{}': {}", source_path, e)

        # Wait before checking for more lines
        stop_event.wait(timeout=FOLLOW_POLL_INTERVAL_SECONDS)


def _tail_source_thread_remote(
    target: EventsTarget,
    source_path: str,
    event_queue: queue.Queue[EventRecord],
    cel_include_filters: Sequence[Any],
    cel_exclude_filters: Sequence[Any],
    stop_event: threading.Event,
    initial_byte_offset: int,
) -> None:
    """Thread function that polls a remote source for new events."""
    byte_offset = initial_byte_offset
    relative_file_path = f"{source_path}/{_EVENTS_JSONL_FILENAME}" if source_path else _EVENTS_JSONL_FILENAME

    while not stop_event.is_set():
        try:
            content = read_event_content(target, relative_file_path)
        except (MngrError, OSError) as e:
            logger.trace("Failed to read remote source '{}' during follow: {}", source_path, e)
            stop_event.wait(timeout=FOLLOW_POLL_INTERVAL_SECONDS)
            continue

        content_bytes = content.encode("utf-8")
        current_length = len(content_bytes)

        if current_length < byte_offset:
            # File was rotated -- re-read from beginning, dedup via event_ids
            logger.debug("Remote event file for source '{}' was rotated", source_path)
            byte_offset = 0

        if current_length > byte_offset:
            new_content = content_bytes[byte_offset:].decode("utf-8", errors="replace")
            for line in new_content.split("\n"):
                record = parse_event_line(line, source_path)
                if record is None:
                    continue
                if not _event_passes_cel_filters(record, cel_include_filters, cel_exclude_filters):
                    continue
                event_queue.put(record)
            byte_offset = current_length

        stop_event.wait(timeout=FOLLOW_POLL_INTERVAL_SECONDS)


_QUEUE_POLL_INTERVAL_SECONDS: Final[float] = 0.1


def _consume_event_queue(
    target_holder: list[EventsTarget],
    state: _AllEventsStreamState,
    event_queue: queue.Queue[EventRecord],
    on_event: Callable[[EventRecord], None],
    cel_include_filters: Sequence[Any],
    cel_exclude_filters: Sequence[Any],
    stop_event: threading.Event,
    tail_threads: list[threading.Thread],
    offset_dir_path: Path | None,
    source_filters: Sequence[str] = (),
) -> None:
    """Consume events from the queue, periodically re-scanning for new sources and checking online/offline."""
    state.last_source_scan_time = time.monotonic()
    last_online_check_time = time.monotonic()

    while not stop_event.is_set():
        # Drain available events from the queue
        try:
            event = event_queue.get(timeout=_QUEUE_POLL_INTERVAL_SECONDS)
        except queue.Empty:
            now = time.monotonic()

            # Periodically re-scan for new source directories
            if now - state.last_source_scan_time > SOURCE_SCAN_INTERVAL_SECONDS:
                _rescan_and_start_new_tail_threads(
                    target=target_holder[0],
                    state=state,
                    event_queue=event_queue,
                    cel_include_filters=cel_include_filters,
                    cel_exclude_filters=cel_exclude_filters,
                    stop_event=stop_event,
                    tail_threads=tail_threads,
                    offset_dir_path=offset_dir_path,
                    source_filters=source_filters,
                )
                state.last_source_scan_time = now

            # Periodically check for online/offline transitions
            if now - last_online_check_time > ONLINE_CHECK_INTERVAL_SECONDS:
                _handle_online_offline_transition(
                    target_holder=target_holder,
                    state=state,
                    event_queue=event_queue,
                    cel_include_filters=cel_include_filters,
                    cel_exclude_filters=cel_exclude_filters,
                    stop_event=stop_event,
                    tail_threads=tail_threads,
                    offset_dir_path=offset_dir_path,
                )
                last_online_check_time = now

            continue

        if event.event_id in state.emitted_event_ids:
            continue
        state.emitted_event_ids.add(event.event_id)
        _maybe_emit_source_mismatch_warning(event, state.warned_incorrect_sources, on_event)
        on_event(event)


def _rescan_and_start_new_tail_threads(
    target: EventsTarget,
    state: _AllEventsStreamState,
    event_queue: queue.Queue[EventRecord],
    cel_include_filters: Sequence[Any],
    cel_exclude_filters: Sequence[Any],
    stop_event: threading.Event,
    tail_threads: list[threading.Thread],
    offset_dir_path: Path | None,
    source_filters: Sequence[str] = (),
) -> None:
    """Re-scan for new event source directories and start tail threads for them."""
    try:
        current_sources = filter_sources_by_name(discover_event_sources(target), source_filters)
    except (MngrError, OSError) as e:
        logger.trace("Failed to re-scan event sources: {}", e)
        return

    for source in current_sources:
        if source.source_path in state.known_source_paths:
            continue

        # New source discovered -- read its historical events and start tailing
        logger.debug("Discovered new event source during follow: {}", source.source_path)
        state.known_source_paths.add(source.source_path)
        state.known_rotated_files[source.source_path] = set(source.rotated_files)

        # Read historical events from this new source
        events, byte_offsets = read_all_historical_events(target, [source], cel_include_filters, cel_exclude_filters)
        for event in events:
            if event.event_id not in state.emitted_event_ids:
                event_queue.put(event)

        # Start a tail thread for the new source
        if source.is_current_file_present and offset_dir_path is not None:
            thread = _start_tail_thread(
                target=target,
                source_path=source.source_path,
                event_queue=event_queue,
                cel_include_filters=cel_include_filters,
                cel_exclude_filters=cel_exclude_filters,
                stop_event=stop_event,
                offset_dir_path=offset_dir_path,
                initial_byte_offset=byte_offsets.get(source.source_path, 0),
            )
            tail_threads.append(thread)


# =============================================================================
# Online/offline transitions
# =============================================================================


def refresh_events_target(
    target: EventsTarget,
) -> EventsTarget:
    """Re-check whether the host is online/offline and return an updated EventsTarget."""
    if target.provider is None or target.host_id is None or target.events_subpath is None:
        return target

    online_host, events_path = _try_get_online_host_for_events(target.provider, target.host_id, target.events_subpath)

    return EventsTarget(
        volume=target.volume,
        online_host=online_host,
        events_path=events_path,
        display_name=target.display_name,
        provider=target.provider,
        host_id=target.host_id,
        events_subpath=target.events_subpath,
    )


def _handle_online_offline_transition(
    target_holder: list[EventsTarget],
    state: _AllEventsStreamState,
    event_queue: queue.Queue[EventRecord],
    cel_include_filters: Sequence[Any],
    cel_exclude_filters: Sequence[Any],
    stop_event: threading.Event,
    tail_threads: list[threading.Thread],
    offset_dir_path: Path | None,
) -> None:
    """Check for online/offline transitions and restart tail threads if needed.

    When the host transitions between online and offline states, the existing
    tail threads are stopped and new ones are started with the updated target.
    Event deduplication via emitted_event_ids ensures no events are emitted twice.
    """
    target = target_holder[0]
    try:
        new_target = refresh_events_target(target)
    except (MngrError, OSError) as e:
        logger.trace("Failed to check online status: {}", e)
        return

    was_online = state.is_online
    is_now_online = new_target.online_host is not None

    if was_online == is_now_online:
        return

    logger.debug(
        "Target {} {}",
        target.display_name,
        "came online" if is_now_online else "went offline",
    )
    state.is_online = is_now_online
    target_holder[0] = new_target

    # Stop existing tail threads so they can be restarted with the new target
    stop_event.set()
    for thread in tail_threads:
        thread.join(timeout=5.0)
    tail_threads.clear()

    # Reset the stop event for the new threads
    stop_event.clear()

    # Restart tail threads for all known sources with the new target
    if offset_dir_path is not None:
        for source_path in state.known_source_paths:
            thread = _start_tail_thread(
                target=new_target,
                source_path=source_path,
                event_queue=event_queue,
                cel_include_filters=cel_include_filters,
                cel_exclude_filters=cel_exclude_filters,
                stop_event=stop_event,
                offset_dir_path=offset_dir_path,
                initial_byte_offset=0,
            )
            tail_threads.append(thread)
