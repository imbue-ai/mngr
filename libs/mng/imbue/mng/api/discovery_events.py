import json
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Any
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
from imbue.mng.hosts.host import Host
from imbue.mng.interfaces.data_types import AgentDetails
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import DiscoveredAgent
from imbue.mng.primitives import DiscoveredHost
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import ProviderInstanceName

DISCOVERY_EVENT_SOURCE: Final[EventSource] = EventSource("mng/discovery")


class DiscoveryEventType(UpperCaseStrEnum):
    """Type of discovery event."""

    AGENT_DISCOVERED = auto()
    HOST_DISCOVERED = auto()
    DISCOVERY_FULL = auto()


# === Event Data Types ===


class AgentDiscoveryEvent(EventEnvelope):
    """A discovery event recording a single agent state change."""

    agent: DiscoveredAgent = Field(description="The discovered agent data")


class HostDiscoveryEvent(EventEnvelope):
    """A discovery event recording a single host state change."""

    host: DiscoveredHost = Field(description="The discovered host data")


class FullDiscoverySnapshotEvent(EventEnvelope):
    """A full snapshot of all agents and hosts from a complete discovery scan."""

    agents: tuple[DiscoveredAgent, ...] = Field(description="All discovered agents")
    hosts: tuple[DiscoveredHost, ...] = Field(description="All discovered hosts")


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
    """Convert an AgentDetails to a lightweight DiscoveredAgent."""
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


@pure
def build_discovered_agent(
    agent_id: AgentId,
    agent_name: AgentName,
    host_id: HostId,
    provider_name: ProviderInstanceName,
    certified_data: dict[str, Any] | None = None,
) -> DiscoveredAgent:
    """Build a DiscoveredAgent from individual fields."""
    return DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=agent_name,
        provider_name=provider_name,
        certified_data=certified_data or {},
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


@pure
def _get_provider_name_from_host(host: OnlineHostInterface) -> ProviderInstanceName:
    """Extract the provider instance name from a host object."""
    if isinstance(host, Host):
        return host.provider_instance.name
    return ProviderInstanceName("unknown")


def safe_emit_agent_discovered(
    config: MngConfig,
    agent_id: AgentId,
    agent_name: AgentName,
    host: OnlineHostInterface,
) -> None:
    """Build and emit an agent discovery event, logging and swallowing any errors.

    This is the standard integration point for commands that modify agents.
    Extracts provider_name from the host automatically.
    It never raises -- failures are logged at trace level.
    """
    try:
        discovered = build_discovered_agent(
            agent_id=agent_id,
            agent_name=agent_name,
            host_id=host.id,
            provider_name=_get_provider_name_from_host(host),
        )
        emit_agent_discovered(config, discovered)
    except OSError as e:
        logger.trace("Failed to emit agent discovery event: {}", e)


def emit_host_discovered(config: MngConfig, host: DiscoveredHost) -> None:
    """Build and append a host discovery event."""
    event = make_host_discovery_event(host)
    append_discovery_event(config, event)
    logger.trace("Emitted host_discovered event for {}", host.host_name)


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


@pure
def parse_discovery_event_line(
    line: str,
) -> AgentDiscoveryEvent | HostDiscoveryEvent | FullDiscoverySnapshotEvent | None:
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
        case DiscoveryEventType.DISCOVERY_FULL:
            return FullDiscoverySnapshotEvent.model_validate(data)
        case _:
            return None


def find_latest_full_snapshot_offset(events_path: Path) -> int:
    """Scan the events file to find the byte offset of the latest DISCOVERY_FULL event.

    Returns 0 if no full snapshot event is found (meaning the entire file should be read).
    """
    if not events_path.exists():
        return 0

    # Read all lines and find the last DISCOVERY_FULL line offset
    last_full_offset = 0
    current_offset = 0
    with open(events_path) as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                try:
                    data = json.loads(stripped)
                    if data.get("type") == DiscoveryEventType.DISCOVERY_FULL:
                        last_full_offset = current_offset
                except json.JSONDecodeError:
                    pass
            current_offset += len(line)

    return last_full_offset


def extract_agents_and_hosts_from_full_listing(
    agent_details_list: Sequence[AgentDetails],
) -> tuple[tuple[DiscoveredAgent, ...], tuple[DiscoveredHost, ...]]:
    """Extract deduplicated DiscoveredAgent and DiscoveredHost tuples from AgentDetails."""
    discovered_agents = tuple(discovered_agent_from_agent_details(a) for a in agent_details_list)

    # Deduplicate hosts by host_id
    seen_host_ids: set[str] = set()
    discovered_hosts: list[DiscoveredHost] = []
    for agent_details in agent_details_list:
        host_id_str = str(agent_details.host.id)
        if host_id_str not in seen_host_ids:
            seen_host_ids.add(host_id_str)
            discovered_hosts.append(discovered_host_from_agent_details(agent_details))

    return discovered_agents, tuple(discovered_hosts)
