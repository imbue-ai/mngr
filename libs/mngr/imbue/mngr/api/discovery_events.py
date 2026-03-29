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
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from imbue.imbue_common.pure import pure
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SSHInfo

DISCOVERY_EVENT_SOURCE: Final[EventSource] = EventSource("mngr/discovery")


class DiscoveryEventType(UpperCaseStrEnum):
    """Type of discovery event."""

    AGENT_DISCOVERED = auto()
    HOST_DISCOVERED = auto()
    AGENT_DESTROYED = auto()
    HOST_DESTROYED = auto()
    DISCOVERY_FULL = auto()
    HOST_SSH_INFO = auto()


# === Event Data Types ===


class AgentDiscoveryEvent(EventEnvelope):
    """A discovery event recording a single agent state change."""

    agent: DiscoveredAgent = Field(description="The discovered agent data")


class HostDiscoveryEvent(EventEnvelope):
    """A discovery event recording a single host state change."""

    host: DiscoveredHost = Field(description="The discovered host data")


class AgentDestroyedEvent(EventEnvelope):
    """A discovery event recording that an agent was destroyed."""

    agent_id: AgentId = Field(description="ID of the destroyed agent")
    host_id: HostId = Field(description="ID of the host the agent was on")


class HostDestroyedEvent(EventEnvelope):
    """A discovery event recording that a host was destroyed."""

    host_id: HostId = Field(description="ID of the destroyed host")
    agent_ids: tuple[AgentId, ...] = Field(description="IDs of agents that were on the host")


class FullDiscoverySnapshotEvent(EventEnvelope):
    """A full snapshot of all agents and hosts from a complete discovery scan."""

    agents: tuple[DiscoveredAgent, ...] = Field(description="All discovered agents")
    hosts: tuple[DiscoveredHost, ...] = Field(description="All discovered hosts")


class HostSSHInfoEvent(EventEnvelope):
    """Records SSH connection info for a host."""

    host_id: HostId = Field(description="ID of the host")
    ssh: SSHInfo = Field(description="SSH connection info for the host")


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
        type=EventType(DiscoveryEventType.AGENT_DISCOVERED),
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        agent=agent,
    )


def make_host_discovery_event(host: DiscoveredHost) -> HostDiscoveryEvent:
    """Build a host discovery event."""
    timestamp, event_id = _make_envelope_fields()
    return HostDiscoveryEvent(
        timestamp=timestamp,
        type=EventType(DiscoveryEventType.HOST_DISCOVERED),
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
        type=EventType(DiscoveryEventType.DISCOVERY_FULL),
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        agents=tuple(agents),
        hosts=tuple(hosts),
    )


# === File I/O ===


def append_discovery_event(config: MngrConfig, event: EventEnvelope) -> None:
    """Append a single discovery event to the JSONL file.

    Creates parent directories if they do not exist. Uses a single write() call
    for safe concurrent appending under PIPE_BUF.
    """
    events_path = get_discovery_events_path(config)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event.model_dump(mode="json"), separators=(",", ":")) + "\n"
    with open(events_path, "a") as f:
        f.write(line)


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
        type=EventType(DiscoveryEventType.AGENT_DESTROYED),
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
        type=EventType(DiscoveryEventType.HOST_DESTROYED),
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
        type=EventType(DiscoveryEventType.HOST_SSH_INFO),
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        host_id=host_id,
        ssh=ssh,
    )
    append_discovery_event(config, event)
    logger.trace("Emitted host_ssh_info event for {}", host_id)


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
    except (MngrError, OSError, ValueError) as e:
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


DiscoveryEvent = (
    AgentDiscoveryEvent
    | HostDiscoveryEvent
    | AgentDestroyedEvent
    | HostDestroyedEvent
    | FullDiscoverySnapshotEvent
    | HostSSHInfoEvent
)


@pure
def parse_discovery_event_line(line: str) -> DiscoveryEvent | None:
    """Parse a single JSONL line into the appropriate discovery event type.

    Returns None if the line cannot be parsed or is not a recognized discovery event.
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None

    event_type = data.get("type")
    match event_type:
        case DiscoveryEventType.AGENT_DISCOVERED:
            return AgentDiscoveryEvent.model_validate(data)
        case DiscoveryEventType.HOST_DISCOVERED:
            return HostDiscoveryEvent.model_validate(data)
        case DiscoveryEventType.AGENT_DESTROYED:
            return AgentDestroyedEvent.model_validate(data)
        case DiscoveryEventType.HOST_DESTROYED:
            return HostDestroyedEvent.model_validate(data)
        case DiscoveryEventType.DISCOVERY_FULL:
            return FullDiscoverySnapshotEvent.model_validate(data)
        case DiscoveryEventType.HOST_SSH_INFO:
            return HostSSHInfoEvent.model_validate(data)
        case _:
            return None


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
    with open(events_path, "rb") as f:
        for raw_line in f:
            line_start = f.tell() - len(raw_line)
            stripped = raw_line.strip()
            if stripped:
                try:
                    data = json.loads(stripped)
                    if data.get("type") == DiscoveryEventType.DISCOVERY_FULL:
                        last_full_offset = line_start
                except json.JSONDecodeError as e:
                    logger.trace("Skipped malformed JSONL line in discovery events: {}", e)

    return last_full_offset


def resolve_provider_names_for_identifiers(
    config: MngrConfig,
    identifiers: Sequence[str],
) -> tuple[str, ...] | None:
    """Resolve agent identifiers to the provider names that own them using the event stream.

    Reads the latest DISCOVERY_FULL snapshot and replays incremental events to build
    agent_name -> set[provider_name] and agent_id -> provider_name mappings.

    Returns the deduplicated union of provider names for all identifiers, or None if
    any identifier cannot be resolved (meaning a full scan is needed).
    """
    events_path = get_discovery_events_path(config)
    if not events_path.exists():
        return None

    # Find the latest full snapshot and replay from there
    offset = find_latest_full_snapshot_offset(events_path)

    # Maps for resolution
    providers_by_agent_name: dict[str, set[str]] = {}
    provider_by_agent_id: dict[str, str] = {}
    destroyed_agent_ids: set[str] = set()

    try:
        with open(events_path) as f:
            f.seek(offset)
            for line in f:
                event = parse_discovery_event_line(line)
                if event is None:
                    continue
                if isinstance(event, FullDiscoverySnapshotEvent):
                    # Reset maps -- this snapshot supersedes everything before it
                    providers_by_agent_name.clear()
                    provider_by_agent_id.clear()
                    destroyed_agent_ids.clear()
                    for agent in event.agents:
                        name_str = str(agent.agent_name)
                        id_str = str(agent.agent_id)
                        prov = str(agent.provider_name)
                        providers_by_agent_name.setdefault(name_str, set()).add(prov)
                        provider_by_agent_id[id_str] = prov
                elif isinstance(event, AgentDiscoveryEvent):
                    agent = event.agent
                    name_str = str(agent.agent_name)
                    id_str = str(agent.agent_id)
                    prov = str(agent.provider_name)
                    providers_by_agent_name.setdefault(name_str, set()).add(prov)
                    provider_by_agent_id[id_str] = prov
                    destroyed_agent_ids.discard(id_str)
                elif isinstance(event, AgentDestroyedEvent):
                    destroyed_agent_ids.add(str(event.agent_id))
                else:
                    # Host events and other types are not relevant for provider resolution
                    pass
    except (OSError, ValueError) as e:
        logger.trace("Failed to read discovery events for provider resolution: {}", e)
        return None

    # Remove destroyed agents from the id map
    for destroyed_id in destroyed_agent_ids:
        provider_by_agent_id.pop(destroyed_id, None)

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
    emitted_event_ids: set[str],
    emit_lock: Lock,
    on_line: Callable[[str], None] | None,
) -> None:
    """Parse and emit a single JSONL line, deduplicating by event_id."""
    stripped = line.strip()
    if not stripped:
        return
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.trace("Skipped malformed JSONL line in discovery event stream")
        return
    event_id = data.get("event_id")
    with emit_lock:
        if event_id and event_id in emitted_event_ids:
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
    on_line: Callable[[str], None] | None,
) -> None:
    """Poll the events file for new content written by other mngr processes."""
    current_offset = initial_offset
    while not stop_event.is_set():
        try:
            if events_path.exists():
                file_size = events_path.stat().st_size
                # Handle file truncation (reset to start)
                if file_size < current_offset:
                    current_offset = 0
                if file_size > current_offset:
                    with open(events_path) as f:
                        f.seek(current_offset)
                        new_content = f.read()
                        current_offset = f.tell()
                    for file_line in new_content.splitlines():
                        if stop_event.is_set():
                            break
                        _discovery_stream_emit_line(file_line, emitted_event_ids, emit_lock, on_line)
        except OSError as e:
            logger.trace("OSError while tailing discovery events file: {}", e)
        stop_event.wait(timeout=1.0)


def _write_unfiltered_full_snapshot(mngr_ctx: MngrContext, error_behavior: ErrorBehavior) -> None:
    """Run an unfiltered list to trigger a full discovery snapshot event.

    The snapshot is written as a side effect of list_agents when the listing is
    unfiltered and error-free. This function exists to trigger that side effect
    explicitly (e.g. for the discovery stream's periodic re-polls).
    """
    from imbue.mngr.api.list import list_agents

    list_agents(
        mngr_ctx=mngr_ctx,
        is_streaming=False,
        error_behavior=error_behavior,
        reset_caches=True,
    )


def _write_unfiltered_full_snapshot_logged(mngr_ctx: MngrContext, error_behavior: ErrorBehavior) -> None:
    """Run an unfiltered full snapshot, logging any errors instead of raising."""
    try:
        _write_unfiltered_full_snapshot(mngr_ctx, error_behavior)
    except (MngrError, OSError) as e:
        logger.warning("Failed to write discovery snapshot: {}", e)


def _on_stream_thread_failure(e: BaseException, stop_event: threading.Event) -> None:
    logger.error("Discovery stream thread failed: {}", e)
    stop_event.set()


def run_discovery_stream(
    mngr_ctx: MngrContext,
    error_behavior: ErrorBehavior,
    on_line: Callable[[str], None] | None = None,
) -> None:
    """Stream discovery events as JSONL.

    Snapshots are always unfiltered so they can be used for state reconstruction.

    1. Emit from the latest cached snapshot on disk (instant, if available)
    2. Run a full sync in the background to update the event stream
    3. Tail the events file for new events written by the background sync or other processes
    4. Periodically re-poll (unfiltered) and write new full snapshots

    If on_line is None, events are written to stdout. Otherwise, the callback
    is called with each deduplicated JSONL line.
    """
    stop_event = threading.Event()
    with ConcurrencyGroup(name="discovery_stream", exit_timeout_seconds=6.0) as cg:
        events_path = get_discovery_events_path(mngr_ctx.config)
        emitted_event_ids: set[str] = set()
        emit_lock = Lock()

        # Phase 1: emit from the latest cached snapshot on disk (fast path)
        has_cached_snapshot = False
        if events_path.exists():
            snapshot_offset = find_latest_full_snapshot_offset(events_path)
            if snapshot_offset > 0:
                has_cached_snapshot = True
                with open(events_path) as f:
                    f.seek(snapshot_offset)
                    for line in f:
                        _discovery_stream_emit_line(line, emitted_event_ids, emit_lock, on_line)

        # Record the current file position for tailing
        initial_offset = events_path.stat().st_size if events_path.exists() else 0

        # Phase 2: start tailing the events file for new events
        cg.start_new_thread(
            target=_discovery_stream_tail_events_file,
            args=(events_path, initial_offset, stop_event, emitted_event_ids, emit_lock, on_line),
            name="discovery_stream_tail",
            on_failure=lambda e: _on_stream_thread_failure(e, stop_event),
        )

        # Phase 3: run the initial full sync
        # If we had a cached snapshot, run this in the background so the caller sees results immediately.
        # If no cached snapshot exists (first run), we must wait for it before we have anything to show.
        if has_cached_snapshot:
            cg.start_new_thread(
                target=_write_unfiltered_full_snapshot_logged,
                args=(mngr_ctx, error_behavior),
                name="discovery_stream_initial_sync",
                on_failure=lambda e: _on_stream_thread_failure(e, stop_event),
            )
        else:
            _write_unfiltered_full_snapshot_logged(mngr_ctx, error_behavior)
            # Emit whatever the sync just wrote (the tail thread may not have picked it up yet)
            if events_path.exists():
                snapshot_offset = find_latest_full_snapshot_offset(events_path)
                with open(events_path) as f:
                    f.seek(snapshot_offset)
                    for line in f:
                        _discovery_stream_emit_line(line, emitted_event_ids, emit_lock, on_line)

        # Phase 4: periodically re-poll (unfiltered) and write full snapshots
        try:
            while not stop_event.is_set():
                stop_event.wait(timeout=_DISCOVERY_STREAM_POLL_INTERVAL_SECONDS)
                if stop_event.is_set():
                    break
                try:
                    _write_unfiltered_full_snapshot(mngr_ctx, error_behavior)
                    # The tail thread will pick up the new snapshot and emit it
                except (MngrError, OSError) as e:
                    logger.warning("Discovery stream poll failed (continuing): {}", e)
        except KeyboardInterrupt:
            pass
        finally:
            stop_event.set()
