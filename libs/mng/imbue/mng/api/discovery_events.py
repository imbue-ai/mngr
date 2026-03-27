import json
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from imbue.imbue_common.pure import pure
from imbue.mng.config.data_types import MngConfig
from imbue.mng.errors import MngError
from imbue.mng.interfaces.data_types import AgentDetails
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import DiscoveredAgent
from imbue.mng.primitives import DiscoveredHost
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import SSHInfo

DISCOVERY_EVENT_SOURCE: Final[EventSource] = EventSource("mng/discovery")


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
def get_discovery_events_dir(config: MngConfig) -> Path:
    """Return the directory for discovery event files."""
    host_dir = Path(config.default_host_dir).expanduser()
    return host_dir / "events" / "mng" / "discovery"


@pure
def get_discovery_events_path(config: MngConfig) -> Path:
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


def append_discovery_event(config: MngConfig, event: EventEnvelope) -> None:
    """Append a single discovery event to the JSONL file.

    Creates parent directories if they do not exist. Uses a single write() call
    for safe concurrent appending under PIPE_BUF.
    """
    events_path = get_discovery_events_path(config)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event.model_dump(mode="json"), separators=(",", ":")) + "\n"
    with open(events_path, "a") as f:
        f.write(line)


def emit_agent_discovered(config: MngConfig, agent: DiscoveredAgent) -> None:
    """Build and append an agent discovery event."""
    event = make_agent_discovery_event(agent)
    append_discovery_event(config, event)
    logger.trace("Emitted agent_discovered event for {}", agent.agent_name)


def emit_host_discovered(config: MngConfig, host: DiscoveredHost) -> None:
    """Build and append a host discovery event."""
    event = make_host_discovery_event(host)
    append_discovery_event(config, event)
    logger.trace("Emitted host_discovered event for {}", host.host_name)


def emit_agent_destroyed(config: MngConfig, agent_id: AgentId, host_id: HostId) -> None:
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
    config: MngConfig,
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


def emit_host_ssh_info(config: MngConfig, host_id: HostId, ssh: SSHInfo) -> None:
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
    config: MngConfig,
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
    except (MngError, OSError, ValueError) as e:
        logger.warning("Failed to emit discovery events: {}", e)


def write_full_discovery_snapshot(
    config: MngConfig,
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
