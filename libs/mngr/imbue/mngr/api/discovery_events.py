import json
import sys
import threading
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from threading import Lock
from typing import Annotated
from typing import Final
from typing import Literal

from loguru import logger
from pydantic import Discriminator
from pydantic import Field
from pydantic import TypeAdapter
from pydantic import ValidationError
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.logging import cleanup_old_rotated_files
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from imbue.imbue_common.logging import generate_rotation_timestamp
from imbue.imbue_common.logging import rotation_lock
from imbue.imbue_common.pure import pure
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import BaseMngrError
from imbue.mngr.errors import DiscoverySchemaChangedError
from imbue.mngr.errors import ProviderDiscoveryError
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SSHInfo
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner
from imbue.mngr.utils.jsonl_warn import split_complete_lines

DISCOVERY_EVENT_SOURCE: Final[EventSource] = EventSource("mngr/discovery")


class DiscoveryEventType(UpperCaseStrEnum):
    """Type of discovery event."""

    AGENT_DISCOVERED = auto()
    HOST_DISCOVERED = auto()
    AGENT_DESTROYED = auto()
    HOST_DESTROYED = auto()
    DISCOVERY_FULL = auto()
    HOST_SSH_INFO = auto()
    DISCOVERY_ERROR = auto()


# === Event Data Types ===


class AgentDiscoveryEvent(EventEnvelope):
    """A discovery event recording a single agent state change."""

    type: Literal[DiscoveryEventType.AGENT_DISCOVERED] = DiscoveryEventType.AGENT_DISCOVERED
    agent: DiscoveredAgent = Field(description="The discovered agent data")


class HostDiscoveryEvent(EventEnvelope):
    """A discovery event recording a single host state change."""

    type: Literal[DiscoveryEventType.HOST_DISCOVERED] = DiscoveryEventType.HOST_DISCOVERED
    host: DiscoveredHost = Field(description="The discovered host data")


class AgentDestroyedEvent(EventEnvelope):
    """A discovery event recording that an agent was destroyed."""

    type: Literal[DiscoveryEventType.AGENT_DESTROYED] = DiscoveryEventType.AGENT_DESTROYED
    agent_id: AgentId = Field(description="ID of the destroyed agent")
    host_id: HostId = Field(description="ID of the host the agent was on")


class HostDestroyedEvent(EventEnvelope):
    """A discovery event recording that a host was destroyed."""

    type: Literal[DiscoveryEventType.HOST_DESTROYED] = DiscoveryEventType.HOST_DESTROYED
    host_id: HostId = Field(description="ID of the destroyed host")
    agent_ids: tuple[AgentId, ...] = Field(description="IDs of agents that were on the host")


class FullDiscoverySnapshotEvent(EventEnvelope):
    """A full snapshot of all agents and hosts from a complete discovery scan."""

    type: Literal[DiscoveryEventType.DISCOVERY_FULL] = DiscoveryEventType.DISCOVERY_FULL
    agents: tuple[DiscoveredAgent, ...] = Field(description="All discovered agents")
    hosts: tuple[DiscoveredHost, ...] = Field(description="All discovered hosts")


class HostSSHInfoEvent(EventEnvelope):
    """Records SSH connection info for a host."""

    type: Literal[DiscoveryEventType.HOST_SSH_INFO] = DiscoveryEventType.HOST_SSH_INFO
    host_id: HostId = Field(description="ID of the host")
    ssh: SSHInfo = Field(description="SSH connection info for the host")


class DiscoveryErrorEvent(EventEnvelope):
    """Records an error encountered during discovery."""

    type: Literal[DiscoveryEventType.DISCOVERY_ERROR] = DiscoveryEventType.DISCOVERY_ERROR
    error_type: str = Field(description="The type name of the exception (e.g. 'RuntimeError')")
    error_message: str = Field(description="The error message")
    source_name: str = Field(description="Provider, host, or agent that caused the error")
    provider_name: str | None = Field(
        default=None,
        description=(
            "Provider instance whose discovery raised, when the error is attributable "
            "to a single provider. Lets consumers (e.g. minds) act per-provider without "
            "parsing source_name."
        ),
    )


DiscoveryEvent = Annotated[
    AgentDiscoveryEvent
    | HostDiscoveryEvent
    | AgentDestroyedEvent
    | HostDestroyedEvent
    | FullDiscoverySnapshotEvent
    | HostSSHInfoEvent
    | DiscoveryErrorEvent,
    Discriminator("type"),
]

_DISCOVERY_EVENT_ADAPTER: Final[TypeAdapter[DiscoveryEvent]] = TypeAdapter(DiscoveryEvent)


# === Path Helpers ===


@pure
def get_discovery_events_dir(config: MngrConfig) -> Path:
    """Return the directory for discovery event files."""
    host_dir = Path(config.default_host_dir).expanduser()
    return host_dir / "events" / "mngr" / "discovery"


@pure
def get_discovery_events_path(config: MngrConfig) -> Path:
    """Return the path to the discovery events JSONL file."""
    return get_discovery_events_dir(config) / "events.jsonl"


# === Conversion Helpers ===


@pure
def discovered_agent_from_agent_details(agent_details: AgentDetails) -> DiscoveredAgent:
    """Convert an AgentDetails to a DiscoveredAgent with full certified_data."""
    return DiscoveredAgent(
        host_id=agent_details.host.id,
        agent_id=agent_details.id,
        agent_name=agent_details.name,
        provider_name=agent_details.host.provider_name,
        certified_data={
            "type": agent_details.type,
            "work_dir": str(agent_details.work_dir),
            "command": str(agent_details.command),
            "create_time": agent_details.create_time.isoformat(),
            "start_on_boot": agent_details.start_on_boot,
            "labels": agent_details.labels,
        },
    )


@pure
def discovered_host_from_agent_details(agent_details: AgentDetails) -> DiscoveredHost:
    """Extract a DiscoveredHost from an AgentDetails."""
    return DiscoveredHost(
        host_id=agent_details.host.id,
        host_name=HostName(agent_details.host.name),
        provider_name=agent_details.host.provider_name,
        host_state=agent_details.host.state,
    )


def _build_ssh_info_from_host(host: OnlineHostInterface) -> SSHInfo | None:
    """Build SSHInfo from an online host's SSH connection info, or None for local hosts."""
    ssh_connection = host.get_ssh_connection_info()
    if ssh_connection is None:
        return None
    user, hostname, port, key_path = ssh_connection
    return SSHInfo(
        user=user,
        host=hostname,
        port=port,
        key_path=key_path,
        command=f"ssh -i {key_path} -p {port} {user}@{hostname}",
    )


@pure
def discovered_host_from_online_host(
    host: OnlineHostInterface,
    provider_name: ProviderInstanceName,
) -> DiscoveredHost:
    """Build a DiscoveredHost from an online host interface."""
    certified = host.get_certified_data()
    return DiscoveredHost(
        host_id=host.id,
        host_name=HostName(certified.host_name),
        provider_name=provider_name,
        host_state=HostState.RUNNING,
    )


# === Event Construction ===


def _make_envelope_fields() -> tuple[IsoTimestamp, EventId]:
    """Generate the standard envelope fields for a new event."""
    timestamp = IsoTimestamp(format_nanosecond_iso_timestamp(datetime.now(timezone.utc)))
    event_id = EventId(generate_log_event_id())
    return timestamp, event_id


def make_agent_discovery_event(agent: DiscoveredAgent) -> AgentDiscoveryEvent:
    """Build an agent discovery event."""
    timestamp, event_id = _make_envelope_fields()
    return AgentDiscoveryEvent(
        timestamp=timestamp,
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        agent=agent,
    )


def make_host_discovery_event(host: DiscoveredHost) -> HostDiscoveryEvent:
    """Build a host discovery event."""
    timestamp, event_id = _make_envelope_fields()
    return HostDiscoveryEvent(
        timestamp=timestamp,
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        host=host,
    )


def make_full_discovery_snapshot_event(
    agents: Sequence[DiscoveredAgent],
    hosts: Sequence[DiscoveredHost],
) -> FullDiscoverySnapshotEvent:
    """Build a full discovery snapshot event."""
    timestamp, event_id = _make_envelope_fields()
    return FullDiscoverySnapshotEvent(
        timestamp=timestamp,
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        agents=tuple(agents),
        hosts=tuple(hosts),
    )


# === File I/O ===


_DISCOVERY_MAX_FILE_SIZE_BYTES: Final[int] = 50 * 1024 * 1024
_DISCOVERY_MAX_ROTATED_COUNT: Final[int] = 1


def append_discovery_event(config: MngrConfig, event: EventEnvelope) -> None:
    """Append a single discovery event to the JSONL file.

    Creates parent directories if they do not exist. Uses a single write() call
    for safe concurrent appending under PIPE_BUF. Rotates the file when it
    exceeds _DISCOVERY_MAX_FILE_SIZE_BYTES.
    """
    events_path = get_discovery_events_path(config)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_discovery_events_if_needed(events_path)
    line = json.dumps(event.model_dump(mode="json"), separators=(",", ":")) + "\n"
    with open(events_path, "a") as f:
        f.write(line)


def _rotate_discovery_events_if_needed(events_path: Path) -> None:
    """Rotate the discovery events file if it exceeds the size limit."""
    try:
        file_size = events_path.stat().st_size
    except OSError:
        return
    if file_size < _DISCOVERY_MAX_FILE_SIZE_BYTES:
        return
    with rotation_lock(events_path.parent):
        # Re-check actual size: another process may have already rotated
        try:
            actual_size = events_path.stat().st_size
        except OSError:
            return
        if actual_size < _DISCOVERY_MAX_FILE_SIZE_BYTES:
            return
        timestamp = generate_rotation_timestamp()
        rotated = events_path.with_name(f"{events_path.name}.{timestamp}")
        try:
            events_path.rename(rotated)
        except OSError as e:
            logger.trace("Failed to rotate discovery events file: {}", e)
            return
        cleanup_old_rotated_files(events_path.parent, _DISCOVERY_MAX_ROTATED_COUNT)


def emit_agent_discovered(config: MngrConfig, agent: DiscoveredAgent) -> None:
    """Build and append an agent discovery event."""
    event = make_agent_discovery_event(agent)
    append_discovery_event(config, event)
    logger.trace("Emitted agent_discovered event for {}", agent.agent_name)


def emit_host_discovered(config: MngrConfig, host: DiscoveredHost) -> None:
    """Build and append a host discovery event."""
    event = make_host_discovery_event(host)
    append_discovery_event(config, event)
    logger.trace("Emitted host_discovered event for {}", host.host_name)


def emit_agent_destroyed(config: MngrConfig, agent_id: AgentId, host_id: HostId) -> None:
    """Build and append an agent destroyed event."""
    timestamp, event_id = _make_envelope_fields()
    event = AgentDestroyedEvent(
        timestamp=timestamp,
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        agent_id=agent_id,
        host_id=host_id,
    )
    append_discovery_event(config, event)
    logger.trace("Emitted agent_destroyed event for {}", agent_id)


def emit_host_destroyed(
    config: MngrConfig,
    host_id: HostId,
    agent_ids: Sequence[AgentId],
) -> None:
    """Build and append a host destroyed event."""
    timestamp, event_id = _make_envelope_fields()
    event = HostDestroyedEvent(
        timestamp=timestamp,
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        host_id=host_id,
        agent_ids=tuple(agent_ids),
    )
    append_discovery_event(config, event)
    logger.trace("Emitted host_destroyed event for {}", host_id)


def emit_host_ssh_info(config: MngrConfig, host_id: HostId, ssh: SSHInfo) -> None:
    """Build and append a host SSH info event."""
    timestamp, event_id = _make_envelope_fields()
    event = HostSSHInfoEvent(
        timestamp=timestamp,
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        host_id=host_id,
        ssh=ssh,
    )
    append_discovery_event(config, event)
    logger.trace("Emitted host_ssh_info event for {}", host_id)


def emit_discovery_error_event(
    config: MngrConfig,
    error_type: str,
    error_message: str,
    source_name: str,
    provider_name: str | None = None,
) -> None:
    """Build and append a discovery error event."""
    timestamp, event_id = _make_envelope_fields()
    event = DiscoveryErrorEvent(
        timestamp=timestamp,
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        error_type=error_type,
        error_message=error_message,
        source_name=source_name,
        provider_name=provider_name,
    )
    append_discovery_event(config, event)
    logger.trace("Emitted discovery_error event: {} from {}", error_type, source_name)


def emit_discovery_error_to_stdout(
    error_type: str,
    error_message: str,
    source_name: str,
    provider_name: str | None = None,
) -> None:
    """Write a discovery error event as a JSONL line to stdout.

    Used in contexts where the events_base_dir is not available (e.g. list.py).
    The discovery stream tail thread will pick it up from stdout.
    """
    timestamp, event_id = _make_envelope_fields()
    event = DiscoveryErrorEvent(
        timestamp=timestamp,
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        error_type=error_type,
        error_message=error_message,
        source_name=source_name,
        provider_name=provider_name,
    )
    line = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def emit_discovery_events_for_host(
    config: MngrConfig,
    host: OnlineHostInterface,
    provider_name: ProviderInstanceName | None = None,
) -> None:
    """Emit agent and host discovery events by reading current state from the host.

    Re-reads the agent data from the host's filesystem to ensure the emitted
    events contain full certified_data. Also emits a host discovery event.

    If provider_name is not provided, it is inferred from the host's discovered
    agents (each DiscoveredAgent carries its provider_name).

    Errors are caught and logged at warning level so that event emission
    never causes the parent command to fail.
    """
    try:
        # Read agent data once and reuse for both provider_name inference and event emission
        discovered_agents = host.discover_agents()

        # Infer provider_name from the host's agents if not provided
        if provider_name is None:
            if discovered_agents:
                provider_name = discovered_agents[0].provider_name
            else:
                provider_name = ProviderInstanceName("unknown")
                logger.debug("Could not infer provider_name for host {} (no agents), using 'unknown'", host.id)

        # Emit host event
        discovered_host = discovered_host_from_online_host(host, provider_name)
        emit_host_discovered(config, discovered_host)

        # Emit SSH info event if this is a remote host
        ssh_info = _build_ssh_info_from_host(host)
        if ssh_info is not None:
            emit_host_ssh_info(config, host.id, ssh_info)

        # Emit agent events with full certified_data from the host's filesystem
        for discovered_agent in discovered_agents:
            emit_agent_discovered(config, discovered_agent)
    except (BaseMngrError, OSError, ValueError) as e:
        logger.warning("Failed to emit discovery events: {}", e)


def write_full_discovery_snapshot(
    config: MngrConfig,
    agents: Sequence[DiscoveredAgent],
    hosts: Sequence[DiscoveredHost],
) -> FullDiscoverySnapshotEvent:
    """Build and append a full discovery snapshot event. Returns the event."""
    event = make_full_discovery_snapshot_event(agents, hosts)
    append_discovery_event(config, event)
    logger.trace(
        "Emitted discovery_full event with {} agent(s) and {} host(s)",
        len(agents),
        len(hosts),
    )
    return event


# === Event Parsing ===


def parse_discovery_event_line(line: str) -> DiscoveryEvent | None:
    """Parse a single JSONL line into the appropriate discovery event type.

    Returns None only for fully empty / whitespace-only lines (these are a
    routine artifact of trailing newlines and EOF; not an error).

    Raises ``json.JSONDecodeError`` for malformed JSON and
    ``DiscoverySchemaChangedError`` for any structurally-valid JSON line that
    does not match a known discovery event type or whose fields have evolved
    out of sync with the current schema. Both conditions represent something
    upstream that has gone wrong and need to surface; silently dropping such
    lines would just mask the underlying problem.
    """
    stripped = line.strip()
    if not stripped:
        return None

    data = json.loads(stripped)

    event_type = data.get("type")
    try:
        return _DISCOVERY_EVENT_ADAPTER.validate_python(data)
    except ValidationError as e:
        raise DiscoverySchemaChangedError(str(event_type), str(e)) from e


def find_latest_full_snapshot_offset(events_path: Path) -> int:
    """Scan the events file to find the byte offset of the latest DISCOVERY_FULL event.

    Returns 0 if no full snapshot event is found (meaning the entire file should be read).
    """
    if not events_path.exists():
        return 0

    # Read all lines and find the last DISCOVERY_FULL line byte offset.
    # Use f.tell() to track byte positions rather than len(line) which counts
    # characters and would be wrong for multi-byte UTF-8 content.
    last_full_offset = 0
    warner = MalformedJsonLineWarner(source_description=f"discovery events file '{events_path}'")
    with open(events_path, "rb") as f:
        for raw_line in f:
            line_start = f.tell() - len(raw_line)
            decoded = raw_line.decode("utf-8", errors="replace")
            parsed = warner.parse(decoded)
            if parsed is None:
                continue
            data, _ = parsed
            if data.get("type") == DiscoveryEventType.DISCOVERY_FULL:
                last_full_offset = line_start

    return last_full_offset


def _replay_discovery_events_for_resolution(
    events_path: Path,
) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """Replay events from the latest full snapshot into resolution maps.

    Returns ``(provider_by_agent_id, name_by_agent_id, destroyed_agent_ids)``.
    Raises DiscoverySchemaChangedError if any event line in the file fails
    schema validation (the caller is responsible for regenerating and retrying).
    Raises OSError on file I/O failure.
    """
    offset = find_latest_full_snapshot_offset(events_path)
    provider_by_agent_id: dict[str, str] = {}
    name_by_agent_id: dict[str, str] = {}
    destroyed_agent_ids: set[str] = set()

    warner = MalformedJsonLineWarner(source_description=f"discovery events file '{events_path}'")
    with open(events_path) as f:
        f.seek(offset)
        for line in f:
            parsed = warner.parse(line)
            if parsed is None:
                continue
            data, stripped_line = parsed
            event = parse_discovery_event_line(stripped_line)
            if isinstance(event, FullDiscoverySnapshotEvent):
                # Reset maps -- this snapshot supersedes everything before it
                provider_by_agent_id.clear()
                name_by_agent_id.clear()
                destroyed_agent_ids.clear()
                for agent in event.agents:
                    id_str = str(agent.agent_id)
                    provider_by_agent_id[id_str] = str(agent.provider_name)
                    name_by_agent_id[id_str] = str(agent.agent_name)
            elif isinstance(event, AgentDiscoveryEvent):
                agent = event.agent
                id_str = str(agent.agent_id)
                provider_by_agent_id[id_str] = str(agent.provider_name)
                name_by_agent_id[id_str] = str(agent.agent_name)
                destroyed_agent_ids.discard(id_str)
            elif isinstance(event, AgentDestroyedEvent):
                destroyed_agent_ids.add(str(event.agent_id))
            else:
                # Host events and other types are not relevant for provider resolution
                pass

    return provider_by_agent_id, name_by_agent_id, destroyed_agent_ids


def resolve_provider_names_for_identifiers(
    mngr_ctx: MngrContext,
    identifiers: Sequence[str],
) -> tuple[str, ...] | None:
    """Resolve agent identifiers to the provider names that own them using the event stream.

    Reads the latest DISCOVERY_FULL snapshot and replays incremental events to build
    agent_name -> set[provider_name] and agent_id -> provider_name mappings.

    Returns the deduplicated union of provider names for all identifiers, or None if
    any identifier cannot be resolved (meaning a full scan is needed).

    If the on-disk events are stale relative to the current model schema, this triggers
    a full discovery scan (which appends fresh events in the current schema), then
    retries parsing once. If parsing still fails, the schema mismatch reflects a real
    bug rather than stale data, so DiscoverySchemaChangedError is re-raised.
    """
    events_path = get_discovery_events_path(mngr_ctx.config)
    if not events_path.exists():
        return None

    try:
        provider_by_agent_id, name_by_agent_id, destroyed_agent_ids = _replay_discovery_events_for_resolution(
            events_path
        )
    except DiscoverySchemaChangedError as e:
        logger.warning("Discovery event schema mismatch; regenerating snapshot and retrying ({})", e)
        # _write_unfiltered_full_snapshot retries a few times internally on
        # transient errors, so the previously-flagged "rare failure during
        # upgrade if schema shifted and a transient error happened" is now
        # less brittle.
        _write_unfiltered_full_snapshot(mngr_ctx)
        # after we've regenerated the list, we should no longer get the DiscoverySchemaChangedError anymore
        provider_by_agent_id, name_by_agent_id, destroyed_agent_ids = _replay_discovery_events_for_resolution(
            events_path
        )

    # Remove destroyed agents from both maps
    for destroyed_id in destroyed_agent_ids:
        provider_by_agent_id.pop(destroyed_id, None)
        name_by_agent_id.pop(destroyed_id, None)

    # Build the name -> providers map from surviving agents
    providers_by_agent_name: dict[str, set[str]] = {}
    for id_str, prov in provider_by_agent_id.items():
        name_str = name_by_agent_id.get(id_str)
        if name_str is not None:
            providers_by_agent_name.setdefault(name_str, set()).add(prov)

    # Resolve each identifier
    resolved_providers: set[str] = set()
    for identifier in identifiers:
        # Try as agent ID first
        if identifier in provider_by_agent_id:
            resolved_providers.add(provider_by_agent_id[identifier])
        # Then try as agent name
        elif identifier in providers_by_agent_name:
            resolved_providers.update(providers_by_agent_name[identifier])
        else:
            # Unknown identifier -- fall back to full scan
            logger.debug(
                f"Could not resolve provider for identifier '{identifier}' from discovery events; full scan needed"
            )
            return None

    return tuple(sorted(resolved_providers))


def extract_agents_and_hosts_from_full_listing(
    agent_details_list: Sequence[AgentDetails],
) -> tuple[tuple[DiscoveredAgent, ...], tuple[DiscoveredHost, ...], tuple[tuple[HostId, SSHInfo], ...]]:
    """Extract deduplicated DiscoveredAgent, DiscoveredHost, and SSH info tuples from AgentDetails."""
    discovered_agents = tuple(discovered_agent_from_agent_details(a) for a in agent_details_list)

    # Deduplicate hosts by host_id, collecting SSH info along the way
    seen_host_ids: set[HostId] = set()
    discovered_hosts: list[DiscoveredHost] = []
    host_ssh_infos: list[tuple[HostId, SSHInfo]] = []
    for agent_details in agent_details_list:
        if agent_details.host.id not in seen_host_ids:
            seen_host_ids.add(agent_details.host.id)
            discovered_hosts.append(discovered_host_from_agent_details(agent_details))
            if agent_details.host.ssh is not None:
                host_ssh_infos.append((agent_details.host.id, agent_details.host.ssh))

    return discovered_agents, tuple(discovered_hosts), tuple(host_ssh_infos)


# === Discovery Stream ===

_DISCOVERY_STREAM_POLL_INTERVAL_SECONDS: Final[float] = 10.0


def _discovery_stream_emit_line(
    line: str,
    warner: MalformedJsonLineWarner,
    emitted_event_ids: set[str],
    emit_lock: Lock,
    on_line: Callable[[str], None] | None,
) -> None:
    """Parse and emit a single JSONL line, deduplicating by event_id."""
    parsed = warner.parse(line)
    if parsed is None:
        return
    data, stripped = parsed
    event_id = data.get("event_id")
    event_type = data.get("type", "unknown")
    with emit_lock:
        if event_id and event_id in emitted_event_ids:
            logger.trace("Discovery stream: skipping already-emitted event {} (type={})", event_id, event_type)
            return
        if event_id:
            emitted_event_ids.add(event_id)
        if on_line is not None:
            on_line(stripped)
        else:
            sys.stdout.write(stripped + "\n")
            sys.stdout.flush()


def _discovery_stream_tail_events_file(
    events_path: Path,
    initial_offset: int,
    stop_event: threading.Event,
    emitted_event_ids: set[str],
    emit_lock: Lock,
    warner: MalformedJsonLineWarner,
    on_line: Callable[[str], None] | None,
) -> None:
    """Poll the events file for new content written by other mngr processes."""
    current_offset = initial_offset
    while not stop_event.is_set():
        try:
            if events_path.exists():
                file_size = events_path.stat().st_size
                # Handle file truncation (reset to start). Drop any malformed
                # line still buffered in the warner: it came from the
                # pre-truncation file's tail, so treating it as mid-file
                # corruption in the new content would be misleading.
                if file_size < current_offset:
                    logger.debug(
                        "Discovery events file truncated (size {} < offset {}), resetting", file_size, current_offset
                    )
                    current_offset = 0
                    warner.reset()
                if file_size > current_offset:
                    with open(events_path) as f:
                        f.seek(current_offset)
                        new_content = f.read()
                    # Hold back any trailing partial line so a mid-flush write
                    # doesn't get split across polls and silently lost.
                    new_lines, bytes_consumed = split_complete_lines(new_content)
                    current_offset += bytes_consumed
                    logger.debug(
                        "Discovery tail: consumed {} new bytes, {} lines from events file",
                        bytes_consumed,
                        len(new_lines),
                    )
                    for file_line in new_lines:
                        if stop_event.is_set():
                            break
                        _discovery_stream_emit_line(file_line, warner, emitted_event_ids, emit_lock, on_line)
        except Exception as e:
            logger.opt(exception=e).error("Error while tailing discovery events file")
        stop_event.wait(timeout=1.0)


def _emit_lines_from_offset(
    events_path: Path,
    offset: int,
    warner: MalformedJsonLineWarner,
    emitted_event_ids: set[str],
    emit_lock: Lock,
    on_line: Callable[[str], None] | None,
) -> int:
    """Read the events file from `offset` to EOF and feed every complete line through the warner.

    Used for the synchronous read phases of run_discovery_stream so that they
    share a single warner instance with the tail thread, which lets a malformed
    line buffered in one phase still surface a warning when the next phase or
    the tail reads more data after it.

    Holds back any trailing partial line (no terminating newline) so a
    mid-flush write doesn't get split between this phase and the tail thread,
    which would silently lose the event and produce misleading mid-file
    corruption warnings about its two halves. Returns the byte position up to
    which the file was actually consumed; callers should use this as the
    starting offset for subsequent reads (e.g. the tail thread).
    """
    with open(events_path, "rb") as f:
        f.seek(offset)
        new_content = f.read().decode("utf-8", errors="replace")
    lines, bytes_consumed = split_complete_lines(new_content)
    for line in lines:
        _discovery_stream_emit_line(line, warner, emitted_event_ids, emit_lock, on_line)
    return offset + bytes_consumed


_DISCOVERY_SNAPSHOT_RETRY_ATTEMPTS: Final[int] = 3
_DISCOVERY_SNAPSHOT_RETRY_INITIAL_WAIT_SECONDS: Final[float] = 0.5
_DISCOVERY_SNAPSHOT_RETRY_MAX_WAIT_SECONDS: Final[float] = 4.0


# FIXME: rework this to do "per-provider" full discovery so a single flaky
# provider can't poison the whole snapshot. The current design does one all-
# providers list_agents under ABORT semantics: if any provider raises, the
# whole snapshot is skipped (after retries). That's the right correctness
# trade-off given list_agents has no notion of "partial-but-trusted" results,
# but it means a single chronically-broken provider blocks discovery for
# everyone. The right fix is for discovery to scan each provider
# independently, emit per-provider AGENT_DISCOVERED / HOST_DISCOVERED
# events, and reconstruct the union state in the consumer rather than relying
# on whole-world DISCOVERY_FULL events.
@retry(
    retry=retry_if_exception_type(BaseMngrError),
    stop=stop_after_attempt(_DISCOVERY_SNAPSHOT_RETRY_ATTEMPTS),
    wait=wait_exponential(
        multiplier=1,
        min=_DISCOVERY_SNAPSHOT_RETRY_INITIAL_WAIT_SECONDS,
        max=_DISCOVERY_SNAPSHOT_RETRY_MAX_WAIT_SECONDS,
    ),
    reraise=True,
)
def _write_unfiltered_full_snapshot(mngr_ctx: MngrContext) -> None:
    """Run an unfiltered list to trigger a full discovery snapshot event.

    The snapshot is written as a side effect of list_agents when the listing
    is unfiltered and error-free. This function exists to trigger that side
    effect explicitly (e.g. for the discovery stream's periodic re-polls).

    Always uses ``ErrorBehavior.ABORT`` so a single flaky provider can never
    cause a partial DISCOVERY_FULL event to be emitted -- consumers treat
    every full snapshot as authoritative state, so a docker hiccup that
    returns zero docker agents would briefly nuke every docker-hosted
    workspace from the desktop client's view. Better to retry a few times
    (transient SSH / Docker / Modal errors usually heal) and skip the
    snapshot entirely if we still can't get a clean read.
    """
    from imbue.mngr.api.list import list_agents

    list_agents(
        mngr_ctx=mngr_ctx,
        is_streaming=False,
        error_behavior=ErrorBehavior.ABORT,
        reset_caches=True,
    )


def _write_unfiltered_full_snapshot_logged(mngr_ctx: MngrContext) -> None:
    """Run an unfiltered full snapshot, logging any errors instead of raising.

    The underlying ``_write_unfiltered_full_snapshot`` always lists with
    ``ErrorBehavior.ABORT`` (with retries) so partial snapshots are never
    persisted.
    """
    try:
        _write_unfiltered_full_snapshot(mngr_ctx)
    except Exception as e:
        logger.opt(exception=e).error("Failed to write discovery snapshot")
        cause = e.cause if isinstance(e, ProviderDiscoveryError) else e
        provider_name = str(e.provider_name) if isinstance(e, ProviderDiscoveryError) else None
        try:
            emit_discovery_error_event(
                mngr_ctx.config,
                error_type=type(cause).__name__,
                error_message=str(cause),
                source_name="discovery_snapshot",
                provider_name=provider_name,
            )
        except (OSError, ValueError):
            pass


def run_discovery_stream(
    mngr_ctx: MngrContext,
    on_line: Callable[[str], None] | None = None,
) -> None:
    """Stream discovery events as JSONL.

    Snapshots are always unfiltered so they can be used for state reconstruction.
    The underlying ``_write_unfiltered_full_snapshot`` always lists with
    ``ErrorBehavior.ABORT`` (with retries) so a flaky provider can never
    cause a partial DISCOVERY_FULL event to be emitted; consumers can rely
    on every full snapshot being authoritative.

    1. Emit from the latest cached snapshot on disk (instant, if available)
    2. Run a full sync in the background to update the event stream
    3. Tail the events file for new events written by the background sync or other processes
    4. Periodically re-poll (unfiltered) and write new full snapshots

    If on_line is None, events are written to stdout. Otherwise, the callback
    is called with each deduplicated JSONL line.
    """
    events_path = get_discovery_events_path(mngr_ctx.config)
    emitted_event_ids: set[str] = set()
    emit_lock = Lock()
    # One warner per file is shared across all phases (and the tail thread) so
    # a malformed line buffered at the end of one phase still surfaces a
    # warning when the next phase or the tail reads valid data after it.
    warner = MalformedJsonLineWarner(source_description=f"discovery events file '{events_path}'")

    # Phase 1: emit from the latest cached snapshot on disk (fast path)
    has_cached_snapshot = False
    # Default to file size; overridden below to the byte position phase 1
    # actually consumed so the tail thread re-reads any trailing partial line.
    initial_offset = events_path.stat().st_size if events_path.exists() else 0
    if events_path.exists():
        snapshot_offset = find_latest_full_snapshot_offset(events_path)
        if snapshot_offset > 0:
            has_cached_snapshot = True
            initial_offset = _emit_lines_from_offset(
                events_path, snapshot_offset, warner, emitted_event_ids, emit_lock, on_line
            )

    # Phase 2: start tailing the events file for new events
    stop_event = threading.Event()
    tail = threading.Thread(
        target=_discovery_stream_tail_events_file,
        args=(events_path, initial_offset, stop_event, emitted_event_ids, emit_lock, warner, on_line),
        daemon=True,
    )
    tail.start()

    # Phase 3: run the initial full sync
    # If we had a cached snapshot, run this in the background so the caller sees results immediately.
    # If no cached snapshot exists (first run), we must wait for it before we have anything to show.
    if has_cached_snapshot:
        initial_sync = threading.Thread(
            target=_write_unfiltered_full_snapshot_logged,
            args=(mngr_ctx,),
            daemon=True,
        )
        initial_sync.start()
    else:
        _write_unfiltered_full_snapshot_logged(mngr_ctx)
        # Emit whatever the sync just wrote (the tail thread may not have picked it up yet).
        # The return value is intentionally ignored here: the tail thread is already running
        # and tracking its own offset, and dedup via emitted_event_ids covers any overlap.
        if events_path.exists():
            snapshot_offset = find_latest_full_snapshot_offset(events_path)
            _emit_lines_from_offset(events_path, snapshot_offset, warner, emitted_event_ids, emit_lock, on_line)

    # Phase 4: periodically re-poll (unfiltered) and write full snapshots
    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=_DISCOVERY_STREAM_POLL_INTERVAL_SECONDS)
            if stop_event.is_set():
                break
            try:
                _write_unfiltered_full_snapshot(mngr_ctx)
                # The tail thread will pick up the new snapshot and emit it
            except Exception as e:
                logger.opt(exception=e).error("Discovery stream poll failed (continuing)")
                cause = e.cause if isinstance(e, ProviderDiscoveryError) else e
                provider_name = str(e.provider_name) if isinstance(e, ProviderDiscoveryError) else None
                try:
                    emit_discovery_error_event(
                        mngr_ctx.config,
                        error_type=type(cause).__name__,
                        error_message=str(cause),
                        source_name="discovery_poll",
                        provider_name=provider_name,
                    )
                except (OSError, ValueError):
                    pass
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        tail.join(timeout=5.0)
