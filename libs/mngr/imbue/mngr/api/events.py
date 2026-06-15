import hashlib
import json
import queue
import tempfile
import threading
import time
from collections.abc import Callable
from collections.abc import Mapping
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
from imbue.imbue_common.logging import ROTATED_JSONL_PATTERN
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import filter_one_host
from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MalformedJsonlLineError
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.offline_host import try_resolve_readable_host
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.interfaces.host import HostFileReadInterface
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentOrHostAddress
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.utils.cel_utils import apply_cel_filters_to_context
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner
from imbue.mngr.utils.jsonl_warn import split_complete_lines

FOLLOW_POLL_INTERVAL_SECONDS: Final[float] = 1.0
SOURCE_SCAN_INTERVAL_SECONDS: Final[float] = 10.0
ONLINE_CHECK_INTERVAL_SECONDS: Final[float] = 30.0
_EVENTS_JSONL_FILENAME: Final[str] = "events.jsonl"


# =============================================================================
# Data types
# =============================================================================


class EventsTarget(FrozenModel):
    """Resolved target for the events command."""

    host: HostFileReadInterface | None = Field(
        default=None,
        description="Readable host (online host, or a stopped host whose volume is reachable) for reading events",
    )
    events_path: Path | None = Field(
        default=None, description="Absolute path to the events directory under the host's host_dir"
    )
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
    def _validate_host_and_events_path_are_paired(self) -> "EventsTarget":
        """Ensure host and events_path are either both set or both None."""
        is_host_set = self.host is not None
        is_path_set = self.events_path is not None
        if is_host_set != is_path_set:
            raise MngrError("host and events_path must both be set or both be None")
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
        description="Sorted rotated file names, oldest first (e.g. events.jsonl.20260301, events.jsonl.20260302)"
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


def try_build_events_target_for_agent(
    *,
    mngr_ctx: MngrContext,
    agent_id: AgentId,
    agent_name: str,
    host_id: HostId,
    provider_name: ProviderInstanceName,
) -> EventsTarget | None:
    """Build an ``EventsTarget`` for an agent given its identity, or None if unreadable.

    Unlike ``resolve_events_target``, this skips the agent-name discovery step
    -- it assumes the caller already has an ``AgentDetails`` (e.g. from
    ``list_agents``) and just wants the events handle. Returns ``None`` when
    the agent's host has neither a readable volume nor an online interface to
    read events from (rather than raising) so a multi-agent walker can skip the
    agent and continue with the others.
    """
    provider = get_provider_instance(provider_name, mngr_ctx)
    agent_events_subpath = Path("agents") / str(agent_id) / "events"
    host, events_path = _try_get_readable_host_for_events(provider, host_id, agent_events_subpath)
    if host is None:
        return None
    return EventsTarget(
        host=host,
        events_path=events_path,
        display_name=f"agent '{agent_name}'",
        provider=provider,
        host_id=host_id,
        events_subpath=agent_events_subpath,
    )


def resolve_events_target(
    address: AgentOrHostAddress,
    mngr_ctx: MngrContext,
) -> EventsTarget:
    """Resolve an :class:`AgentOrHostAddress` to an :class:`EventsTarget`.

    Agent vs host is decided by the address type (no state-based fallback).
    When the target host is online, the returned :class:`EventsTarget`
    includes the online host and events path for direct command execution
    (e.g. ``tail -f``).
    """
    if isinstance(address, AgentAddress):
        return _resolve_agent_events_target(address, mngr_ctx)
    return _resolve_host_events_target(address, mngr_ctx)


def _resolve_agent_events_target(address: AgentAddress, mngr_ctx: MngrContext) -> EventsTarget:
    host_ref, agent_ref = find_one_agent(address, mngr_ctx)
    with log_span("Getting events access for agent {}", agent_ref.agent_name):
        target = try_build_events_target_for_agent(
            mngr_ctx=mngr_ctx,
            agent_id=agent_ref.agent_id,
            agent_name=str(agent_ref.agent_name),
            host_id=host_ref.host_id,
            provider_name=host_ref.provider_name,
        )
    if target is None:
        raise MngrError(
            f"Provider '{host_ref.provider_name}' does not support volumes and the host is not online. "
            "Cannot read events for this agent."
        )
    return target


def _resolve_host_events_target(address: HostAddress, mngr_ctx: MngrContext) -> EventsTarget:
    # Narrow discovery to the pinned provider when the address has one, so a
    # `@HOST.PROVIDER` target skips unrelated providers (the agent path gets
    # the same treatment via discover_by_address).
    provider_names: tuple[str, ...] | None = (str(address.provider),) if address.provider is not None else None
    host_agents_by_host, _ = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=provider_names,
        agent_identifiers=None,
        include_destroyed=False,
        reset_caches=False,
    )
    all_hosts = list(host_agents_by_host.keys())
    host_ref = filter_one_host(address, all_hosts)

    with log_span("Getting events access for host {}", host_ref.host_name):
        provider = get_provider_instance(host_ref.provider_name, mngr_ctx)

        host_events_subpath = Path("events")
        host, events_path = _try_get_readable_host_for_events(provider, host_ref.host_id, host_events_subpath)

        if host is None:
            raise MngrError(
                f"Provider '{host_ref.provider_name}' does not support volumes and the host is not online. "
                "Cannot read events for this host."
            )

    return EventsTarget(
        host=host,
        events_path=events_path,
        display_name=f"host '{host_ref.host_name}'",
        provider=provider,
        host_id=host_ref.host_id,
        events_subpath=host_events_subpath,
    )


def _try_get_readable_host_for_events(
    provider: BaseProviderInstance,
    host_id: HostId,
    events_subpath: Path,
) -> tuple[HostFileReadInterface | None, Path | None]:
    """Resolve a readable host and the absolute events path under its ``host_dir``.

    Prefers a live online host (so follow-mode can tail locally and remote
    reads use SSH). When the host is not online but its persisted volume is
    reachable, returns a readable offline host so historical events can still
    be read from the volume. Returns ``(None, None)`` when neither is available.

    Delegates the online-or-volume-backed-offline resolution rule to
    :func:`try_resolve_readable_host`; here we only compute the absolute events
    path under the resolved host's ``host_dir``.
    """
    host_interface = try_resolve_readable_host(provider, host_id)
    if host_interface is None:
        return None, None

    # Every readable host resolved here is also a HostInterface (an online host
    # or a volume-backed offline host), so it exposes a real host_dir under which
    # the events path lives.
    if not isinstance(host_interface, HostInterface):
        raise MngrError(f"Resolved readable host for {host_id} does not expose a host_dir")
    events_path = host_interface.host_dir / str(events_subpath)
    return host_interface, events_path


# =============================================================================
# Read event content
# =============================================================================


def read_event_content(target: EventsTarget, event_file_name: str) -> str:
    """Read the full content of an event file, relative to the events directory.

    Reads through :class:`HostFileReadInterface`, whose ``read_file`` is
    byte-exact (local reads bytes directly, remote uses SFTP), so the file's
    exact bytes -- including its trailing-newline state -- are preserved.
    """
    if target.host is None or target.events_path is None:
        raise MngrError(f"Cannot read event file for {target.display_name}: no readable host available")

    file_path = target.events_path / event_file_name
    with log_span("Reading event file '{}' for {}", event_file_name, target.display_name):
        try:
            content_bytes = target.host.read_file(file_path)
        except (FileNotFoundError, OSError) as e:
            raise MngrError(f"Failed to read event file '{event_file_name}': {e}") from e
        return content_bytes.decode("utf-8", errors="replace")


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
def _record_from_event_data(data: Mapping[str, Any], stripped_line: str, source_hint: str) -> EventRecord:
    """Build an EventRecord from already-parsed JSON data, applying source-hint correction.

    The input is a Mapping rather than a dict so the type system enforces that
    this function never mutates caller-owned state: the returned EventRecord's
    `data` field is always a fresh dict.

    Raises ``MalformedJsonlLineError`` when the event JSON is missing required
    envelope fields. Whichever process is producing the bad event needs to be
    fixed -- silently dropping it would just hide the underlying problem.
    """
    timestamp = data.get("timestamp", "")
    if not timestamp:
        raise MalformedJsonlLineError(f"Missing required 'timestamp' field in event JSON: {stripped_line[:200]!r}")

    event_id = data.get("event_id", "")
    if not event_id:
        # Generate deterministic fallback from line content
        event_id = "hash-" + hashlib.sha256(stripped_line.encode()).hexdigest()[:24]

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

    corrected_data = {**data, "source": source_hint}
    # Re-serialize raw_line only when the JSON had a wrong source field;
    # backfilling a missing source doesn't require re-serializing because the
    # original line is still a faithful representation of the event.
    if original_source is not None:
        corrected_raw_line = json.dumps(corrected_data, separators=(",", ":"))
    else:
        corrected_raw_line = stripped_line

    return EventRecord(
        raw_line=corrected_raw_line,
        timestamp=timestamp,
        event_id=event_id,
        source=source_hint,
        data=corrected_data,
        original_source=original_source,
    )


@pure
def parse_event_line(line: str, source_hint: str) -> EventRecord:
    """Parse a single JSONL line into an EventRecord.

    Raises ``json.JSONDecodeError`` on malformed JSON and
    ``MalformedJsonlLineError`` when the line is valid JSON but is not a JSON
    object or is missing required envelope fields. Garbage input is treated as
    a real failure, not a soft skip: callers reading a multi-line stream where
    end-of-file partial writes are expected should use ``MalformedJsonLineWarner``
    instead of calling this directly.
    """
    stripped = line.strip()
    data = json.loads(stripped)
    if not isinstance(data, dict):
        raise MalformedJsonlLineError(
            f"Expected JSON object on event line but got {type(data).__name__}: {stripped[:200]!r}"
        )
    return _record_from_event_data(data, stripped, source_hint)


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
    """Sort rotated file names so oldest (lowest timestamp) comes first.

    Input: ['events.jsonl.20260415130000000000', 'events.jsonl.20260415120000000000']
    Output: ['events.jsonl.20260415120000000000', 'events.jsonl.20260415130000000000']
    """
    timestamped: list[tuple[str, str]] = []
    for name in filenames:
        match = ROTATED_JSONL_PATTERN.match(name)
        if match:
            timestamped.append((match.group(1), name))
    timestamped.sort(key=lambda pair: pair[0])
    return [name for _, name in timestamped]


# =============================================================================
# Event source discovery
# =============================================================================


def discover_event_sources(target: EventsTarget) -> list[EventSourceInfo]:
    """Find all event sources (subdirectories containing events.jsonl files).

    Lists the events directory recursively through the host's
    :class:`HostFileReadInterface`, filters for ``events.jsonl`` and its
    rotated variants (``events.jsonl.<timestamp>``), and groups the matches by
    their directory relative to ``events_path``.
    """
    if target.host is None or target.events_path is None:
        raise MngrError(f"Cannot discover event sources for {target.display_name}: no readable host available")

    with log_span("Discovering event sources for {}", target.display_name):
        entries = target.host.list_directory(target.events_path, recursive=True)
        return _build_event_sources_from_listing(entries, target.events_path)


@pure
def _build_event_sources_from_grouped_files(
    files_by_dir: dict[str, list[str]],
) -> list[EventSourceInfo]:
    """Build EventSourceInfo objects from files grouped by directory."""
    sources: list[EventSourceInfo] = []
    for dir_path, filenames in sorted(files_by_dir.items()):
        rotated = [f for f in filenames if ROTATED_JSONL_PATTERN.match(f)]
        is_current_present = _EVENTS_JSONL_FILENAME in filenames
        sources.append(
            EventSourceInfo(
                source_path=dir_path,
                rotated_files=tuple(_sort_rotated_files_oldest_first(rotated)),
                is_current_file_present=is_current_present,
            )
        )
    return sources


def _build_event_sources_from_listing(
    entries: Sequence[VolumeFile],
    events_path: Path,
) -> list[EventSourceInfo]:
    """Group a recursive directory listing into EventSourceInfo objects.

    Keeps only regular files named ``events.jsonl`` or a rotated variant, and
    groups them by their parent directory relative to ``events_path`` (the
    root events file lives under the empty source path).
    """
    files_by_dir: dict[str, list[str]] = {}
    for entry in entries:
        if entry.file_type != FileType.FILE:
            continue
        absolute = Path(entry.path)
        file_part = absolute.name
        if file_part != _EVENTS_JSONL_FILENAME and not ROTATED_JSONL_PATTERN.match(file_part):
            continue
        try:
            relative_dir = absolute.parent.relative_to(events_path)
        except ValueError:
            continue
        dir_part = "" if str(relative_dir) == "." else str(relative_dir)
        files_by_dir.setdefault(dir_part, []).append(file_part)

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

    Note: this function intentionally does NOT hold back a trailing partial line via
    split_complete_lines. It is used for whole-file historical reads (rotated files
    and the current events.jsonl), where the final line is expected to be complete;
    holding one back would misclassify a complete final line as partial and silently
    drop it. ``read_event_content`` reads byte-exact via ``HostFileReadInterface.read_file``,
    so a file's true trailing-newline state is preserved either way. Partial-write
    robustness during *streaming* is handled separately by the follow-tail loop in
    _read_remote_source_once, which has its own partial-line guard.
    """
    try:
        content = read_event_content(target, relative_file_path)
    except (MngrError, OSError) as e:
        logger.trace("Failed to read event file '{}': {}", relative_file_path, e)
        return [], 0

    events: list[EventRecord] = []
    warner = MalformedJsonLineWarner(source_description=f"event file '{relative_file_path}'")
    for line in content.split("\n"):
        parsed = warner.parse(line)
        if parsed is None:
            continue
        data, stripped = parsed
        events.append(_record_from_event_data(data, stripped, source_hint))

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
    target_holder: list[EventsTarget],
    sources: Sequence[EventSourceInfo],
    initial_byte_offsets: dict[str, int],
    event_queue: queue.Queue[EventRecord],
    cel_include_filters: Sequence[Any],
    cel_exclude_filters: Sequence[Any],
    stop_event: threading.Event,
    online_event: threading.Event,
    offset_dir_path: Path,
) -> list[threading.Thread]:
    """Start one persistent tail thread per current events.jsonl file.

    The threads are started unconditionally (even while offline): each gates its
    own I/O on ``online_event`` and does nothing while the target is offline, so
    a stopped agent costs no reads without any thread teardown/restart churn.
    """
    threads: list[threading.Thread] = []
    for source in sources:
        if source.is_current_file_present:
            thread = _start_tail_thread(
                target_holder=target_holder,
                source_path=source.source_path,
                event_queue=event_queue,
                cel_include_filters=cel_include_filters,
                cel_exclude_filters=cel_exclude_filters,
                stop_event=stop_event,
                online_event=online_event,
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
        is_online=isinstance(target.host, OnlineHostInterface),
        last_source_scan_time=time.monotonic(),
    )
    stop_event = threading.Event()
    # Gates per-source tail I/O: set while online, cleared while offline. The
    # tail threads stay alive across transitions and simply park (no reads)
    # while it is clear, so a stopped agent costs nothing without thread churn.
    online_event = threading.Event()
    if state.is_online:
        online_event.set()
    # Shared, swappable handle to the current target. The consume loop swaps
    # target_holder[0] on an online/offline transition; the tail threads read
    # it each poll, so they follow the new target without being recreated.
    target_holder: list[EventsTarget] = [target]
    event_queue: queue.Queue[EventRecord] = queue.Queue()
    tail_threads: list[threading.Thread] = []
    offset_dir: tempfile.TemporaryDirectory[str] | None = None

    try:
        # Discover sources and read all historical events
        all_events, sources, initial_byte_offsets = _collect_historical_events(
            target, state, cel_include_filters, cel_exclude_filters, source_filters
        )

        # Start one persistent tail thread per source for follow mode. They are
        # started even while offline -- each gates its own I/O on online_event,
        # so an offline target drives no reads, and coming back online just sets
        # the gate (no teardown/restart).
        if is_follow:
            offset_dir = tempfile.TemporaryDirectory(prefix="mngr-events-offsets-")
            tail_threads = _start_tail_threads_for_sources(
                target_holder,
                sources,
                initial_byte_offsets,
                event_queue,
                cel_include_filters,
                cel_exclude_filters,
                stop_event,
                online_event,
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
            target_holder=target_holder,
            state=state,
            event_queue=event_queue,
            on_event=on_event,
            cel_include_filters=cel_include_filters,
            cel_exclude_filters=cel_exclude_filters,
            stop_event=stop_event,
            online_event=online_event,
            tail_threads=tail_threads,
            offset_dir_path=Path(offset_dir.name) if offset_dir is not None else None,
            source_filters=source_filters,
        )

    finally:
        stop_event.set()
        # Wake any thread parked on the offline gate so it observes the stop and
        # exits promptly rather than after a full poll interval.
        online_event.set()
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


def _resolve_read_plan(target: EventsTarget, source_path: str) -> tuple[bool, Path] | None:
    """Return ``(is_local, events_file_path)`` for reading ``source_path`` from ``target`` now.

    ``is_local`` selects the read mechanism: pygtail incremental tailing of a
    local online host's file, versus whole-file polling (an SSH-remote online
    host, or a volume-backed offline host). ``events_file_path`` is the absolute
    path to the source's ``events.jsonl`` and also serves as the switch-detection
    key -- a change in either the mechanism or the path means the tail thread
    must re-initialize its reader. Returns ``None`` when the target exposes no
    readable events path.
    """
    if target.events_path is None:
        return None
    is_local = isinstance(target.host, OnlineHostInterface) and target.host.is_local
    return is_local, target.events_path / source_path / _EVENTS_JSONL_FILENAME


def _start_tail_thread(
    target_holder: list[EventsTarget],
    source_path: str,
    event_queue: queue.Queue[EventRecord],
    cel_include_filters: Sequence[Any],
    cel_exclude_filters: Sequence[Any],
    stop_event: threading.Event,
    online_event: threading.Event,
    offset_dir_path: Path,
    initial_byte_offset: int,
) -> threading.Thread:
    """Start a persistent daemon thread that tails one source into the queue.

    The thread lives for the whole follow session: it reads the current target
    from ``target_holder`` each poll and gates its I/O on ``online_event``, so a
    target going offline/online needs only a flag flip, never a thread restart.
    """
    plan = _resolve_read_plan(target_holder[0], source_path)
    if plan is not None and plan[0]:
        # Local initial plan: pre-write the pygtail offset so the first read
        # resumes exactly where the historical read left off (no gap, no
        # needless re-read). On a later mechanism/path switch the thread itself
        # resets to the start and relies on dedup.
        _write_pygtail_offset_file(plan[1], source_path, offset_dir_path, initial_byte_offset)
    thread = threading.Thread(
        target=_tail_source_thread,
        args=(
            source_path,
            target_holder,
            event_queue,
            cel_include_filters,
            cel_exclude_filters,
            stop_event,
            online_event,
            offset_dir_path,
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


def _read_local_source_once(
    events_file_path: Path,
    source_path: str,
    offset_dir_path: Path,
    warner: MalformedJsonLineWarner,
    event_queue: queue.Queue[EventRecord],
    cel_include_filters: Sequence[Any],
    cel_exclude_filters: Sequence[Any],
    stop_event: threading.Event,
) -> None:
    """Read any new lines from a local events.jsonl via pygtail and enqueue them.

    Pygtail reads from the offset file on construction, so each call picks up
    where the previous one left off (the offset file is pre-written before the
    first call, and reset on a mechanism/path switch).
    """
    offset_file = _pygtail_offset_file_path(source_path, offset_dir_path)
    tail = Pygtail(
        str(events_file_path),
        offset_file=offset_file,
        save_on_end=True,
        read_from_end=False,
        full_lines=True,
        copytruncate=True,
        # Rotated files are named events.jsonl.<timestamp>. Pygtail's built-in
        # patterns only check for .1 and dateext with '-', so we add a custom
        # glob so it can find the rotated file and read any events written
        # between our last read and the rotation.
        log_patterns=["%s.[0-9]*"],
    )
    for line in tail:
        if stop_event.is_set():
            break
        parsed = warner.parse(line)
        if parsed is None:
            continue
        data, stripped = parsed
        record = _record_from_event_data(data, stripped, source_path)
        if not _event_passes_cel_filters(record, cel_include_filters, cel_exclude_filters):
            continue
        event_queue.put(record)


def _read_remote_source_once(
    target: EventsTarget,
    source_path: str,
    byte_offset: int,
    warner: MalformedJsonLineWarner,
    event_queue: queue.Queue[EventRecord],
    cel_include_filters: Sequence[Any],
    cel_exclude_filters: Sequence[Any],
) -> int:
    """Poll a source by whole-file read, enqueue new lines, and return the new byte offset.

    Used for any non-local target (SSH-remote online host, or a volume-backed
    offline host). May raise ``MngrError``/``OSError`` from the read; the caller
    handles that and retries on the next poll without advancing the offset.
    """
    relative_file_path = f"{source_path}/{_EVENTS_JSONL_FILENAME}" if source_path else _EVENTS_JSONL_FILENAME
    content = read_event_content(target, relative_file_path)

    content_bytes = content.encode("utf-8")
    current_length = len(content_bytes)

    if current_length < byte_offset:
        # File was rotated -- re-read from beginning, dedup via event_ids.
        # Drop any malformed line still buffered in the warner: it came from
        # the now-rotated file's tail, so treating it as mid-file corruption
        # in the new file would be misleading.
        logger.debug("Remote event file for source '{}' was rotated", source_path)
        byte_offset = 0
        warner.reset()

    if current_length > byte_offset:
        new_content = content_bytes[byte_offset:].decode("utf-8", errors="replace")
        # Only consume up to the last newline; any trailing partial line is
        # left in the file for the next poll so a mid-flush write doesn't
        # cause the line to be split and silently lost.
        lines, bytes_consumed = split_complete_lines(new_content)
        for line in lines:
            parsed = warner.parse(line)
            if parsed is None:
                continue
            data, stripped = parsed
            record = _record_from_event_data(data, stripped, source_path)
            if not _event_passes_cel_filters(record, cel_include_filters, cel_exclude_filters):
                continue
            event_queue.put(record)
        byte_offset += bytes_consumed

    return byte_offset


def _tail_source_thread(
    source_path: str,
    target_holder: list[EventsTarget],
    event_queue: queue.Queue[EventRecord],
    cel_include_filters: Sequence[Any],
    cel_exclude_filters: Sequence[Any],
    stop_event: threading.Event,
    online_event: threading.Event,
    offset_dir_path: Path,
    initial_byte_offset: int,
) -> None:
    """Persistent per-source tail thread for the whole follow session.

    Does no I/O while offline (``online_event`` clear): a stopped agent's files
    cannot change, so polling them would just burn reads (one ``docker exec``
    each for the Docker provider). On each online poll it reads from the current
    target (``target_holder[0]``), which the consume loop swaps on a transition.
    When the target's read mechanism or path changes, the thread re-initializes
    its reader and re-reads from the start; ``emitted_event_ids`` dedup in the
    consume loop suppresses anything already emitted, so the re-read never
    double-emits.
    """
    warner = MalformedJsonLineWarner(source_description=f"event source '{source_path}'")
    byte_offset = initial_byte_offset
    # Seed the switch-detection key from the target the thread was created
    # against, so the first online poll on the initial target uses the
    # pre-written offset rather than treating it as a switch (which would reset).
    last_plan = _resolve_read_plan(target_holder[0], source_path)

    while not stop_event.is_set():
        # Pause all I/O while offline; wake periodically to re-check shutdown.
        if not online_event.is_set():
            online_event.wait(timeout=FOLLOW_POLL_INTERVAL_SECONDS)
            continue

        plan = _resolve_read_plan(target_holder[0], source_path)
        if plan is None:
            stop_event.wait(timeout=FOLLOW_POLL_INTERVAL_SECONDS)
            continue

        is_local, events_file_path = plan
        if plan != last_plan:
            # Mechanism/path changed (an online/offline transition). Re-read
            # from the start; dedup backstops against re-emitting old events.
            byte_offset = 0
            warner.reset()
            if is_local:
                _write_pygtail_offset_file(events_file_path, source_path, offset_dir_path, 0)
            last_plan = plan

        try:
            if is_local:
                _read_local_source_once(
                    events_file_path,
                    source_path,
                    offset_dir_path,
                    warner,
                    event_queue,
                    cel_include_filters,
                    cel_exclude_filters,
                    stop_event,
                )
            else:
                byte_offset = _read_remote_source_once(
                    target_holder[0],
                    source_path,
                    byte_offset,
                    warner,
                    event_queue,
                    cel_include_filters,
                    cel_exclude_filters,
                )
        except (MngrError, OSError, IOError) as e:
            logger.trace("Tail read error for source '{}': {}", source_path, e)

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
    online_event: threading.Event,
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

            # Periodically re-scan for new source directories. A new source
            # directory only appears when the agent writes a new kind of event,
            # which requires a running (online) host, so while the target is
            # offline we skip the scan and its per-directory listing cost. When
            # the host returns, _handle_online_offline_transition flips the gate
            # back on and the next scan picks up any genuinely new sources.
            if state.is_online and now - state.last_source_scan_time > SOURCE_SCAN_INTERVAL_SECONDS:
                _rescan_and_start_new_tail_threads(
                    target_holder=target_holder,
                    state=state,
                    event_queue=event_queue,
                    cel_include_filters=cel_include_filters,
                    cel_exclude_filters=cel_exclude_filters,
                    stop_event=stop_event,
                    online_event=online_event,
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
                    online_event=online_event,
                )
                last_online_check_time = now

            continue

        if event.event_id in state.emitted_event_ids:
            continue
        state.emitted_event_ids.add(event.event_id)
        _maybe_emit_source_mismatch_warning(event, state.warned_incorrect_sources, on_event)
        on_event(event)


def _rescan_and_start_new_tail_threads(
    target_holder: list[EventsTarget],
    state: _AllEventsStreamState,
    event_queue: queue.Queue[EventRecord],
    cel_include_filters: Sequence[Any],
    cel_exclude_filters: Sequence[Any],
    stop_event: threading.Event,
    online_event: threading.Event,
    tail_threads: list[threading.Thread],
    offset_dir_path: Path | None,
    source_filters: Sequence[str] = (),
) -> None:
    """Re-scan for new event source directories and start tail threads for them."""
    target = target_holder[0]
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

        # Start a persistent tail thread for the new source
        if source.is_current_file_present and offset_dir_path is not None:
            thread = _start_tail_thread(
                target_holder=target_holder,
                source_path=source.source_path,
                event_queue=event_queue,
                cel_include_filters=cel_include_filters,
                cel_exclude_filters=cel_exclude_filters,
                stop_event=stop_event,
                online_event=online_event,
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

    host, events_path = _try_get_readable_host_for_events(target.provider, target.host_id, target.events_subpath)
    if host is None:
        # Neither online nor a readable volume right now -- keep the previous
        # handle rather than producing an unreadable target.
        return target

    return EventsTarget(
        host=host,
        events_path=events_path,
        display_name=target.display_name,
        provider=target.provider,
        host_id=target.host_id,
        events_subpath=target.events_subpath,
    )


def _handle_online_offline_transition(
    target_holder: list[EventsTarget],
    state: _AllEventsStreamState,
    online_event: threading.Event,
) -> None:
    """Detect an online/offline transition and update shared state accordingly.

    On a net change this swaps ``target_holder[0]`` to the refreshed target and
    sets/clears ``online_event``. The persistent tail threads read both on their
    next poll: they follow the new target and either resume reading (online) or
    park doing no I/O (offline). No threads are created or torn down here -- that
    churn, and its teardown races, is gone. Event deduplication via
    ``emitted_event_ids`` ensures no events are emitted twice when tailing
    resumes (a thread re-reads its source from the start after the switch).
    """
    target = target_holder[0]
    try:
        new_target = refresh_events_target(target)
    except (MngrError, OSError) as e:
        logger.trace("Failed to check online status: {}", e)
        return

    was_online = state.is_online
    is_now_online = isinstance(new_target.host, OnlineHostInterface)

    if was_online == is_now_online:
        return

    logger.debug(
        "Target {} {}",
        target.display_name,
        "came online" if is_now_online else "went offline",
    )
    state.is_online = is_now_online
    target_holder[0] = new_target

    if is_now_online:
        online_event.set()
    else:
        online_event.clear()
