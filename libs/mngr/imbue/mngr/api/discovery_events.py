import json
import sys
import threading
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
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

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import cleanup_old_rotated_files
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from imbue.imbue_common.logging import generate_rotation_timestamp
from imbue.imbue_common.logging import rotation_lock
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import AgentNotFoundError
from imbue.mngr.errors import DiscoverySchemaChangedError
from imbue.mngr.errors import MngrError
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


class DiscoveredProvider(FrozenModel):
    """A provider instance that successfully loaded during discovery.

    A provider appears here when ``get_provider_instance(...)`` succeeded
    (construction only). It may still have failed to enumerate any of its
    hosts -- per-host failures continue to log at warning rather than being
    surfaced on the snapshot. Providers whose construction failed end up in
    ``FullDiscoverySnapshotEvent.error_by_provider_name`` instead.
    """

    provider_name: ProviderInstanceName = Field(description="Name of the provider instance")
    # Typed as the base class so subclass-specific fields are dropped on
    # serialization. Consumers (e.g. minds' providers panel) only need the
    # base fields and shouldn't see plugin-internal config details.
    config: ProviderInstanceConfig = Field(description="The provider's base configuration data")


class DiscoveryError(FrozenModel):
    """An error encountered during discovery, attributed to a provider."""

    type_name: str = Field(description="The type name of the exception (e.g. 'RuntimeError')")
    message: str = Field(description="The error message")
    provider_name: ProviderInstanceName = Field(description="The provider instance associated with the error")


class FullDiscoverySnapshotEvent(EventEnvelope):
    """A full snapshot of all agents and hosts from a complete discovery scan.

    Always emitted on every discovery poll, including when zero providers
    succeeded. A snapshot is authoritative *only* for providers that
    succeeded on this poll. Previously-known agents and hosts whose provider
    is in ``error_by_provider_name`` MUST be retained from prior consumer
    state and surfaced as unknown/stale -- their absence from this snapshot
    reflects the errored poll, not a confirmed removal. A retained item is
    dropped only on an explicit destroy event or a subsequent *successful*
    (non-errored) snapshot of its provider that shows it absent. See
    :func:`partition_removed_agents_by_provider_error`, the shared helper
    every consumer uses to make this decision.
    """

    type: Literal[DiscoveryEventType.DISCOVERY_FULL] = DiscoveryEventType.DISCOVERY_FULL
    agents: tuple[DiscoveredAgent, ...] = Field(description="All discovered agents")
    hosts: tuple[DiscoveredHost, ...] = Field(description="All discovered hosts")
    providers: tuple[DiscoveredProvider, ...] = Field(
        default=(),
        description="All providers whose construction succeeded during this discovery scan",
    )
    error_by_provider_name: dict[ProviderInstanceName, DiscoveryError] = Field(
        default_factory=dict,
        description=(
            "Errors keyed by provider name for providers whose discovery raised "
            "(e.g. auth, network, or total-API-failure during discover_hosts_and_agents)"
        ),
    )


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
    """Return the directory for discovery event files.

    Both the snapshot writers (under ``list_agents``) and the reader/tail in
    ``run_discovery_stream`` / ``tail_discovery_events_file`` derive their path from
    this function, so every mngr process on the same host dir reads and writes a
    single shared discovery log.
    """
    return config.default_host_dir.expanduser() / "events" / "mngr" / "discovery"


@pure
def get_discovery_events_path(config: MngrConfig) -> Path:
    """Return the path to the discovery events JSONL file."""
    return get_discovery_events_dir(config) / "events.jsonl"


# === Provider-error retention ===


class RemovedAgentPartition(FrozenModel):
    """How a snapshot's removed agents split by their prior provider's error state.

    ``retained`` are agents absent from the new snapshot whose prior provider
    is currently errored: their state is unknown, not gone, so the consumer
    keeps them. ``dropped`` are agents the consumer should now forget (their
    provider succeeded and still omitted them, or it is unknown).
    """

    retained: frozenset[str] = Field(description="Agent id strings to keep despite absence from the snapshot")
    dropped: frozenset[str] = Field(description="Agent id strings to drop (confirmed gone or unattributable)")


@pure
def partition_removed_agents_by_provider_error(
    removed_agent_ids: AbstractSet[str],
    provider_name_by_prior_agent_id: Mapping[str, str],
    error_by_provider_name: Mapping[ProviderInstanceName, DiscoveryError],
) -> RemovedAgentPartition:
    """Split removed agent ids into those to retain vs drop, by provider error state.

    An agent absent from a fresh snapshot is *retained* when its prior provider
    is in ``error_by_provider_name`` -- the snapshot omitted it only because
    that provider's discovery raised this poll, so its state is unknown rather
    than confirmed gone. Every other removed agent is *dropped*. Shared by all
    discovery-snapshot consumers so the retention rule has exactly one
    definition (see :class:`FullDiscoverySnapshotEvent`).
    """
    errored_provider_names = {str(name) for name in error_by_provider_name}
    retained: set[str] = set()
    dropped: set[str] = set()
    for agent_id_str in removed_agent_ids:
        prior_provider_name = provider_name_by_prior_agent_id.get(agent_id_str)
        if prior_provider_name is not None and prior_provider_name in errored_provider_names:
            retained.add(agent_id_str)
        else:
            dropped.add(agent_id_str)
    return RemovedAgentPartition(retained=frozenset(retained), dropped=frozenset(dropped))


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
            "plugin": dict(agent_details.plugin),
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
    providers: Sequence[DiscoveredProvider] = (),
    error_by_provider_name: Mapping[ProviderInstanceName, DiscoveryError] | None = None,
) -> FullDiscoverySnapshotEvent:
    """Build a full discovery snapshot event."""
    timestamp, event_id = _make_envelope_fields()
    return FullDiscoverySnapshotEvent(
        timestamp=timestamp,
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        agents=tuple(agents),
        hosts=tuple(hosts),
        providers=tuple(providers),
        error_by_provider_name=dict(error_by_provider_name) if error_by_provider_name else {},
    )


def make_discovered_provider(
    provider_name: ProviderInstanceName,
    config: ProviderInstanceConfig,
) -> DiscoveredProvider:
    """Build a DiscoveredProvider with only the base ProviderInstanceConfig fields.

    Constructs a fresh base-class config so that any subclass-specific fields
    on the input (e.g. plugin-defined credentials, workspace IDs) are dropped.
    Pydantic's serialization would also drop them when typed as the base, but
    constructing explicitly here makes the intent obvious and avoids relying
    on serialization-time behavior at every call site.
    """
    return DiscoveredProvider(
        provider_name=provider_name,
        config=ProviderInstanceConfig(
            backend=config.backend,
            plugin=config.plugin,
            is_enabled=config.is_enabled,
            destroyed_host_persisted_seconds=config.destroyed_host_persisted_seconds,
            min_online_host_age_seconds=config.min_online_host_age_seconds,
        ),
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
    providers: Sequence[DiscoveredProvider] = (),
    error_by_provider_name: Mapping[ProviderInstanceName, DiscoveryError] | None = None,
) -> FullDiscoverySnapshotEvent:
    """Build and append a full discovery snapshot event. Returns the event."""
    event = make_full_discovery_snapshot_event(agents, hosts, providers, error_by_provider_name)
    append_discovery_event(config, event)
    logger.trace(
        "Emitted discovery_full event with {} agent(s), {} host(s), {} provider(s), {} provider error(s)",
        len(agents),
        len(hosts),
        len(event.providers),
        len(event.error_by_provider_name),
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


class ResolvedAgentHost(FrozenModel):
    """The host an agent identifier resolves to, as reconstructed from the event stream.

    Carries only what ``mngr stop --stop-host`` needs to fetch and stop the
    host without SSH: the ``provider_name`` to obtain the provider instance and
    the ``host_id`` to look the host up. The host's name and its continued
    existence both come from the provider's (SSH-free) ``get_host`` at stop
    time, so they are not reconstructed here.
    """

    host_id: HostId = Field(description="ID of the host the agent runs on")
    provider_name: ProviderInstanceName = Field(description="Provider instance that owns the host")


class _ResolutionMaps(MutableModel):
    """Bundle of the maps built (and mutated in place) while replaying discovery events."""

    # agent_id -> provider_name
    provider_by_agent_id: dict[str, str] = Field(default_factory=dict)
    # agent_id -> agent_name
    name_by_agent_id: dict[str, str] = Field(default_factory=dict)
    # agent_id -> host_id
    host_id_by_agent_id: dict[str, str] = Field(default_factory=dict)
    # agent ids known to be destroyed
    destroyed_agent_ids: set[str] = Field(default_factory=set)

    def reset(self) -> None:
        """Clear every map -- used when a full snapshot supersedes prior state."""
        self.provider_by_agent_id.clear()
        self.name_by_agent_id.clear()
        self.host_id_by_agent_id.clear()
        self.destroyed_agent_ids.clear()


def _record_agent(maps: _ResolutionMaps, agent: DiscoveredAgent) -> None:
    """Record a single discovered agent into the resolution maps."""
    id_str = str(agent.agent_id)
    maps.provider_by_agent_id[id_str] = str(agent.provider_name)
    maps.name_by_agent_id[id_str] = str(agent.agent_name)
    maps.host_id_by_agent_id[id_str] = str(agent.host_id)
    maps.destroyed_agent_ids.discard(id_str)


def _replay_discovery_events_into_maps(events_path: Path) -> _ResolutionMaps:
    """Replay events from the latest full snapshot into a :class:`_ResolutionMaps`.

    Raises DiscoverySchemaChangedError if any event line in the file fails
    schema validation (the caller is responsible for regenerating and retrying).
    Raises OSError on file I/O failure.
    """
    offset = find_latest_full_snapshot_offset(events_path)
    maps = _ResolutionMaps()

    warner = MalformedJsonLineWarner(source_description=f"discovery events file '{events_path}'")
    with open(events_path) as f:
        f.seek(offset)
        for line in f:
            parsed = warner.parse(line)
            if parsed is None:
                continue
            _data, stripped_line = parsed
            event = parse_discovery_event_line(stripped_line)
            if isinstance(event, FullDiscoverySnapshotEvent):
                # Reset maps -- this snapshot supersedes everything before it
                maps.reset()
                for agent in event.agents:
                    _record_agent(maps, agent)
            elif isinstance(event, AgentDiscoveryEvent):
                _record_agent(maps, event.agent)
            elif isinstance(event, AgentDestroyedEvent):
                maps.destroyed_agent_ids.add(str(event.agent_id))
            else:
                # Host, SSH info, and error events are not relevant for
                # resolution. A host's continued existence (and its name) come
                # from provider.get_host when the caller fetches the host to
                # stop it, so there is no need to replay host events here.
                pass

    return maps


def _replay_discovery_events_for_resolution(
    events_path: Path,
) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """Replay events from the latest full snapshot into provider-resolution maps.

    Returns ``(provider_by_agent_id, name_by_agent_id, destroyed_agent_ids)``.
    Raises DiscoverySchemaChangedError if any event line in the file fails
    schema validation (the caller is responsible for regenerating and retrying).
    Raises OSError on file I/O failure.
    """
    maps = _replay_discovery_events_into_maps(events_path)
    return maps.provider_by_agent_id, maps.name_by_agent_id, maps.destroyed_agent_ids


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
        # _write_unfiltered_full_snapshot uses ErrorBehavior.CONTINUE, so a
        # provider that raises during this regen still produces a fresh
        # snapshot (with the failure surfaced via error_by_provider_name).
        # Transient retries are the providers' responsibility, not this
        # layer's, so a hard failure inside list_agents itself will bubble.
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


def resolve_hosts_for_identifiers(
    mngr_ctx: MngrContext,
    identifiers: Sequence[str],
) -> dict[str, ResolvedAgentHost]:
    """Resolve agent identifiers to the hosts that run them, without any SSH.

    Reads the latest DISCOVERY_FULL snapshot and replays incremental events to
    map each agent identifier (name or ID) to the ``host_id`` and provider
    recorded for it. This deliberately avoids :func:`discover_hosts_and_agents`
    / the base ``discover_agents`` path, which reads each host's agent
    directory over SSH and therefore fails when a host is up but unreachable
    over SSH (e.g. a dead sshd) -- one of the cases ``mngr stop --stop-host``
    is meant to handle, though not the only reason the flag exists.

    Existence of the resolved host is *not* checked here. The caller fetches it
    via the provider's (also SSH-free) ``get_host``, which raises if the host
    is gone and supplies the authoritative host name -- so a single SSH-free
    lookup against the one relevant provider both validates and names the host,
    with no need to scan every provider's hosts up front.

    Returns a map from each input identifier to its :class:`ResolvedAgentHost`.

    Raises :class:`AgentNotFoundError` if any identifier is absent from the
    event stream, has been destroyed, or maps to agents on more than one host
    (which must be disambiguated with ``NAME@HOST.PROVIDER``).

    If the on-disk events are stale relative to the current model schema, this
    regenerates the snapshot once and retries, mirroring
    :func:`resolve_provider_names_for_identifiers`.
    """
    events_path = get_discovery_events_path(mngr_ctx.config)
    if not events_path.exists():
        raise AgentNotFoundError(
            f"Could not resolve a host for: {', '.join(identifiers)} (no discovery event stream available)"
        )

    try:
        maps = _replay_discovery_events_into_maps(events_path)
    except DiscoverySchemaChangedError as e:
        logger.warning("Discovery event schema mismatch; regenerating snapshot and retrying ({})", e)
        _write_unfiltered_full_snapshot(mngr_ctx)
        maps = _replay_discovery_events_into_maps(events_path)

    # Drop destroyed agents so they cannot resolve.
    for destroyed_id in maps.destroyed_agent_ids:
        maps.provider_by_agent_id.pop(destroyed_id, None)
        maps.name_by_agent_id.pop(destroyed_id, None)
        maps.host_id_by_agent_id.pop(destroyed_id, None)

    # Build agent_name -> set of agent_ids for name-based lookup.
    agent_ids_by_name: dict[str, set[str]] = {}
    for agent_id_str, name_str in maps.name_by_agent_id.items():
        agent_ids_by_name.setdefault(name_str, set()).add(agent_id_str)

    resolved: dict[str, ResolvedAgentHost] = {}
    for identifier in identifiers:
        if identifier in maps.provider_by_agent_id:
            candidate_agent_ids = {identifier}
        elif identifier in agent_ids_by_name:
            candidate_agent_ids = agent_ids_by_name[identifier]
        else:
            raise AgentNotFoundError(
                f"Could not resolve a host for agent '{identifier}' from the discovery event stream"
            )

        # Collect the distinct hosts the candidate agent(s) run on. An agent
        # name spanning more than one host_id is ambiguous and must be
        # disambiguated explicitly.
        candidate_hosts: dict[str, ResolvedAgentHost] = {}
        for agent_id_str in candidate_agent_ids:
            host_id_str = maps.host_id_by_agent_id.get(agent_id_str)
            provider_str = maps.provider_by_agent_id.get(agent_id_str)
            if host_id_str is None or provider_str is None:
                continue
            candidate_hosts[host_id_str] = ResolvedAgentHost(
                host_id=HostId(host_id_str),
                provider_name=ProviderInstanceName(provider_str),
            )

        if not candidate_hosts:
            raise AgentNotFoundError(
                f"Could not resolve a host for agent '{identifier}' from the discovery event stream"
            )
        if len(candidate_hosts) > 1:
            host_ids = ", ".join(sorted(candidate_hosts))
            raise AgentNotFoundError(
                f"Agent identifier '{identifier}' matches agents on multiple hosts ({host_ids}); "
                "disambiguate using NAME@HOST.PROVIDER"
            )
        resolved[identifier] = next(iter(candidate_hosts.values()))

    return resolved


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

# Cadence of the discovery polling loop: each tick re-lists and writes a full
# snapshot. Public because consumers (e.g. minds) derive freshness thresholds
# from it -- a snapshot older than a small multiple of this means the pipeline
# has stalled, not that any single provider is down (per-provider failures ride
# along in each snapshot's ``error_by_provider_name``).
DISCOVERY_STREAM_POLL_INTERVAL_SECONDS: Final[float] = 10.0


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


def _emit_latest_cached_snapshot(
    events_path: Path,
    warner: MalformedJsonLineWarner,
    emitted_event_ids: set[str],
    emit_lock: Lock,
    on_line: Callable[[str], None] | None,
) -> tuple[int, bool]:
    """Emit lines from the latest full snapshot on disk, if any.

    Returns the byte offset up to which the file was consumed (the starting offset
    for a subsequent tail) and whether a cached snapshot was actually emitted. When
    the file is absent the offset is 0; when it exists but holds no full snapshot the
    offset is the current file size (so the tail only sees newly-appended lines).
    """
    if not events_path.exists():
        return 0, False
    snapshot_offset = find_latest_full_snapshot_offset(events_path)
    if snapshot_offset <= 0:
        return events_path.stat().st_size, False
    consumed_offset = _emit_lines_from_offset(
        events_path, snapshot_offset, warner, emitted_event_ids, emit_lock, on_line
    )
    return consumed_offset, True


def tail_discovery_events_file(
    events_path: Path,
    stop_event: threading.Event,
    on_line: Callable[[str], None],
) -> None:
    """Emit the latest cached discovery snapshot, then tail the file for appended events.

    A pure *consumer* of an existing discovery event log: unlike ``run_discovery_stream``
    it never polls providers or writes snapshots. Intended for a process that observes
    the discovery stream produced by *another* ``mngr observe --discovery-only`` (e.g.
    ``mngr forward --observe-via-file``). Blocks until ``stop_event`` is set, so callers
    run it on a dedicated thread.

    Tolerates the file being absent when called (the tail loop waits for it to appear)
    and being truncated/rotated while tailing (it resets and re-reads), reusing the same
    tail loop ``run_discovery_stream`` relies on. The latest cached snapshot is emitted
    up front so a consumer attaching mid-stream is populated immediately.
    """
    emitted_event_ids: set[str] = set()
    emit_lock = Lock()
    warner = MalformedJsonLineWarner(source_description=f"discovery events file '{events_path}'")
    # Emit from the latest full snapshot on disk so a consumer attaching mid-stream
    # is populated immediately. ``find_latest_full_snapshot_offset`` returns 0 when
    # the file is absent or holds no snapshot yet *and* when the snapshot is the very
    # first line; reading from that offset covers all three (the dedup set keeps a
    # later real snapshot from double-emitting), so unlike ``run_discovery_stream``'s
    # writer-side fast path we never skip an offset-0 snapshot.
    if events_path.exists():
        snapshot_offset = find_latest_full_snapshot_offset(events_path)
        initial_offset = _emit_lines_from_offset(
            events_path, snapshot_offset, warner, emitted_event_ids, emit_lock, on_line
        )
    else:
        initial_offset = 0
    _discovery_stream_tail_events_file(
        events_path, initial_offset, stop_event, emitted_event_ids, emit_lock, warner, on_line
    )


def _write_unfiltered_full_snapshot(mngr_ctx: MngrContext) -> None:
    """Run an unfiltered list to trigger a full discovery snapshot event.

    The snapshot is written as a side effect of ``list_agents`` when the
    listing is unfiltered. Uses ``ErrorBehavior.CONTINUE`` so per-provider
    failures land in the snapshot's ``error_by_provider_name`` field rather
    than blocking emission of the whole snapshot. The contract is that each
    provider is responsible for retrying its own transient failures before
    raising; no retry layer is applied here.
    """
    from imbue.mngr.api.list import list_agents

    list_agents(
        mngr_ctx=mngr_ctx,
        is_streaming=False,
        error_behavior=ErrorBehavior.CONTINUE,
        reset_caches=True,
    )


def _write_unfiltered_full_snapshot_logged(mngr_ctx: MngrContext) -> None:
    """Run an unfiltered full snapshot, logging any uncaught error instead of raising.

    With ``ErrorBehavior.CONTINUE`` the typical per-provider failure modes are
    handled inside ``list_agents`` and surfaced via the snapshot's
    ``error_by_provider_name`` field. This wrapper exists for the rare case
    where ``list_agents`` itself raises -- e.g. a programming bug or a
    structurally broken config -- so the discovery polling loop can survive
    and the failure is still visible via a ``DiscoveryErrorEvent``.
    """
    try:
        _write_unfiltered_full_snapshot(mngr_ctx)
    except Exception as e:
        logger.opt(exception=e).error("Discovery snapshot write failed (continuing)")
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


def run_discovery_stream(
    mngr_ctx: MngrContext,
    on_line: Callable[[str], None] | None = None,
) -> None:
    """Stream discovery events as JSONL.

    Snapshots are always unfiltered so they can be used for state reconstruction.
    The underlying ``_write_unfiltered_full_snapshot`` lists with
    ``ErrorBehavior.CONTINUE`` so a snapshot is emitted on every poll, even
    when some providers raised. Per-provider failures land in the snapshot's
    ``error_by_provider_name`` field; consumers treat each snapshot as
    authoritative only for providers that succeeded, retaining previously-known
    agents/hosts for any provider that errored on this poll (and surfacing them
    as unknown/stale) rather than dropping them. Providers are responsible for
    retrying their own transient failures before raising -- there is no
    top-level retry layer here.

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

    # Phase 1: emit from the latest cached snapshot on disk (fast path). The
    # returned offset is the byte position actually consumed so the tail thread
    # re-reads any trailing partial line.
    initial_offset, has_cached_snapshot = _emit_latest_cached_snapshot(
        events_path, warner, emitted_event_ids, emit_lock, on_line
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
            stop_event.wait(timeout=DISCOVERY_STREAM_POLL_INTERVAL_SECONDS)
            if stop_event.is_set():
                break
            # Always emits a snapshot on success (including the all-providers-
            # failed case via error_by_provider_name); logs + emits a
            # DiscoveryErrorEvent if list_agents itself raises.
            _write_unfiltered_full_snapshot_logged(mngr_ctx)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        tail.join(timeout=5.0)
