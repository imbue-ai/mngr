import json
import sys
import threading
from collections.abc import Callable
from collections.abc import Mapping
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
from imbue.mngr.api.discovery_reconciliation import is_intervening_event
from imbue.mngr.api.discovery_reconciliation import parse_event_timestamp
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import AgentNotFoundError
from imbue.mngr.errors import DiscoverySchemaChangedError
from imbue.mngr.errors import MngrError
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
    # DEPRECATED: the historical global "snapshot of all agents/hosts from one
    # all-providers discovery scan", emitted by the now-removed run_discovery_stream
    # and mngr list side-effect. Superseded by DISCOVERY_PROVIDER. No longer
    # produced, but kept (do not delete) so historical on-disk discovery logs that
    # contain DISCOVERY_FULL lines still parse -- the discovery models use
    # extra="forbid" + a discriminated union, so removing this constant would make
    # parse_discovery_event_line raise on those old lines.
    DISCOVERY_FULL = auto()
    # A snapshot of a single provider's agents/hosts. Each provider is discovered on
    # its own decoupled loop and emits its own snapshot, so a slow/hung provider
    # cannot block any other provider. Supersedes DISCOVERY_FULL.
    DISCOVERY_PROVIDER = auto()
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
    """DEPRECATED legacy global snapshot of all agents/hosts from one all-providers scan.

    Historically emitted on every discovery poll by ``run_discovery_stream`` and the
    ``mngr list`` side-effect, before discovery became per-provider. Superseded by
    :class:`ProviderDiscoverySnapshotEvent`: a single hung provider could stall this
    whole-world snapshot, which is exactly what per-provider discovery fixes.

    No longer produced. Kept (do NOT delete) only so historical on-disk discovery
    logs that still contain ``DISCOVERY_FULL`` lines continue to parse -- the
    discovery models use ``extra="forbid"`` + a discriminated union, so removing this
    class would make :func:`parse_discovery_event_line` raise on those old lines.
    The shared :class:`DiscoveryStateAggregator` ignores this event type, and
    :func:`_replay_discovery_events_into_maps` tolerates it as a back-compat reset.
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


class ProviderDiscoverySnapshotEvent(EventEnvelope):
    """A snapshot of one provider's agents and hosts from a single discovery poll.

    Will supersede :class:`FullDiscoverySnapshotEvent` once every consumer migrates
    (both currently coexist). Each provider is discovered on its own decoupled loop
    and emits its own snapshot independently, so a slow or hung provider cannot delay
    any other provider's discovery.

    A snapshot is authoritative *only* for ``provider_name`` -- it says nothing
    about any other provider's agents or hosts, so a consumer must scope its
    "what disappeared" diff to this provider's prior agents/hosts only.

    ``error`` is set when this provider's discovery raised this poll; its
    previously-known agents/hosts MUST be retained by consumers (their state is
    unknown, not gone). ``unknown_host_ids`` / ``unknown_agent_ids`` mark items
    whose individual read exceeded the per-host / per-agent sub-provider timeout:
    their state is explicitly unknown, distinct from being destroyed (absent from
    the snapshot) or from a fully-errored provider.

    ``discovery_started_at`` / ``discovery_finished_at`` bound the wall-clock span
    during which this provider's state was read. A consumer must NOT let this
    snapshot overwrite an item whose own state-change / destroy event was observed
    at or after ``discovery_started_at`` -- that event reflects newer truth than
    this in-flight snapshot. See :class:`DiscoveryStateAggregator`.
    """

    type: Literal[DiscoveryEventType.DISCOVERY_PROVIDER] = DiscoveryEventType.DISCOVERY_PROVIDER
    provider_name: ProviderInstanceName = Field(description="The provider this snapshot is authoritative for")
    agents: tuple[DiscoveredAgent, ...] = Field(description="All agents discovered on this provider this poll")
    hosts: tuple[DiscoveredHost, ...] = Field(description="All hosts discovered on this provider this poll")
    provider: DiscoveredProvider | None = Field(
        default=None,
        description="The provider's base configuration, when its construction succeeded this poll",
    )
    error: DiscoveryError | None = Field(
        default=None,
        description="Set when this provider's discovery raised this poll (retain prior agents/hosts as stale)",
    )
    unknown_host_ids: tuple[HostId, ...] = Field(
        default=(),
        description="Hosts whose individual read exceeded the per-host timeout (state explicitly unknown)",
    )
    unknown_agent_ids: tuple[AgentId, ...] = Field(
        default=(),
        description="Agents whose individual read exceeded the per-agent timeout (state explicitly unknown)",
    )
    discovery_started_at: datetime = Field(description="When this provider's discovery poll began")
    discovery_finished_at: datetime = Field(description="When this provider's discovery poll completed")


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
    | ProviderDiscoverySnapshotEvent
    | HostSSHInfoEvent
    | DiscoveryErrorEvent,
    Discriminator("type"),
]

_DISCOVERY_EVENT_ADAPTER: Final[TypeAdapter[DiscoveryEvent]] = TypeAdapter(DiscoveryEvent)


# === Path Helpers ===


@pure
def get_discovery_events_dir(config: MngrConfig) -> Path:
    """Return the directory for discovery event files.

    Both the snapshot writers (the per-provider producer and the ``list_agents``
    side-effect) and the reader/tail (``run_per_provider_discovery_stream`` /
    ``tail_discovery_events_file``) derive their path from this function, so every
    mngr process on the same host dir reads and writes a single shared discovery log.
    """
    return config.default_host_dir.expanduser() / "events" / "mngr" / "discovery"


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


def make_provider_discovery_snapshot_event(
    provider_name: ProviderInstanceName,
    agents: Sequence[DiscoveredAgent],
    hosts: Sequence[DiscoveredHost],
    discovery_started_at: datetime,
    discovery_finished_at: datetime,
    provider: DiscoveredProvider | None = None,
    error: DiscoveryError | None = None,
    unknown_host_ids: Sequence[HostId] = (),
    unknown_agent_ids: Sequence[AgentId] = (),
) -> ProviderDiscoverySnapshotEvent:
    """Build a per-provider discovery snapshot event."""
    timestamp, event_id = _make_envelope_fields()
    return ProviderDiscoverySnapshotEvent(
        timestamp=timestamp,
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        provider_name=provider_name,
        agents=tuple(agents),
        hosts=tuple(hosts),
        provider=provider,
        error=error,
        unknown_host_ids=tuple(unknown_host_ids),
        unknown_agent_ids=tuple(unknown_agent_ids),
        discovery_started_at=discovery_started_at,
        discovery_finished_at=discovery_finished_at,
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
            discovery_poll_interval_seconds=config.discovery_poll_interval_seconds,
            discovery_warn_seconds=config.discovery_warn_seconds,
            discovery_error_timeout_seconds=config.discovery_error_timeout_seconds,
            host_discovery_timeout_seconds=config.host_discovery_timeout_seconds,
            agent_discovery_timeout_seconds=config.agent_discovery_timeout_seconds,
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


def write_provider_discovery_snapshot(
    config: MngrConfig,
    provider_name: ProviderInstanceName,
    agents: Sequence[DiscoveredAgent],
    hosts: Sequence[DiscoveredHost],
    discovery_started_at: datetime,
    discovery_finished_at: datetime,
    provider: DiscoveredProvider | None = None,
    error: DiscoveryError | None = None,
    unknown_host_ids: Sequence[HostId] = (),
    unknown_agent_ids: Sequence[AgentId] = (),
) -> ProviderDiscoverySnapshotEvent:
    """Build and append a per-provider discovery snapshot event. Returns the event."""
    event = make_provider_discovery_snapshot_event(
        provider_name,
        agents,
        hosts,
        discovery_started_at,
        discovery_finished_at,
        provider=provider,
        error=error,
        unknown_host_ids=unknown_host_ids,
        unknown_agent_ids=unknown_agent_ids,
    )
    append_discovery_event(config, event)
    logger.trace(
        "Emitted discovery_provider event for {} with {} agent(s), {} host(s), error={}",
        provider_name,
        len(agents),
        len(hosts),
        error is not None,
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


def _find_earliest_snapshot_window_start(events_path: Path) -> datetime | None:
    """Earliest discovery-span start that a correct replay must reach back to, or None.

    For each provider, a ``DISCOVERY_PROVIDER`` snapshot is authoritative back to its
    ``discovery_started_at`` (when its read began), which is *earlier* than its own line
    position (written at ``discovery_finished_at``). So the value here is the minimum,
    across every provider's *latest* snapshot, of that snapshot's ``discovery_started_at``.

    The legacy ``DISCOVERY_FULL`` timestamp participates only when the file holds no
    per-provider snapshot at all (a pure pre-migration log). Nothing writes
    ``DISCOVERY_FULL`` anymore and consumers drop its content, so once any per-provider
    snapshot exists the full line is pure history; letting it participate in the minimum
    would permanently pin the replay window at the last pre-migration write, replaying
    days of stale events on every attach. Returns None when the file holds no snapshot
    of either kind.
    """
    latest_start_by_provider: dict[str, datetime] = {}
    latest_full_started_at: datetime | None = None
    warner = MalformedJsonLineWarner(source_description=f"discovery events file '{events_path}'")
    with open(events_path) as f:
        for line in f:
            parsed = warner.parse(line)
            if parsed is None:
                continue
            data, _ = parsed
            event_type = data.get("type")
            if event_type == DiscoveryEventType.DISCOVERY_PROVIDER:
                started_at_raw = data.get("discovery_started_at")
                if started_at_raw is not None:
                    latest_start_by_provider[str(data.get("provider_name", ""))] = parse_event_timestamp(
                        IsoTimestamp(str(started_at_raw))
                    )
            elif event_type == DiscoveryEventType.DISCOVERY_FULL:
                # The legacy global snapshot has no span; its own write time is the
                # furthest back its (whole-world) reset needs replaying from.
                timestamp_raw = data.get("timestamp")
                if timestamp_raw is not None:
                    latest_full_started_at = parse_event_timestamp(IsoTimestamp(str(timestamp_raw)))
            else:
                pass

    if latest_start_by_provider:
        return min(latest_start_by_provider.values())
    return latest_full_started_at


def _find_offset_of_first_event_at_or_after(events_path: Path, start: datetime) -> int:
    """Byte offset of the first event line whose timestamp is at or after ``start`` (0 if none).

    Uses ``f.tell()`` to track byte positions rather than ``len(line)``, which counts
    characters and would be wrong for multi-byte UTF-8 content.
    """
    warner = MalformedJsonLineWarner(source_description=f"discovery events file '{events_path}'")
    with open(events_path, "rb") as f:
        for raw_line in f:
            line_start = f.tell() - len(raw_line)
            decoded = raw_line.decode("utf-8", errors="replace")
            parsed = warner.parse(decoded)
            if parsed is None:
                continue
            data, _ = parsed
            timestamp_raw = data.get("timestamp")
            if timestamp_raw is None:
                continue
            if parse_event_timestamp(IsoTimestamp(str(timestamp_raw))) >= start:
                return line_start
    return 0


def find_discovery_snapshot_replay_offset(events_path: Path) -> int:
    """Byte offset to replay from so a reader reconstructs current state without re-reading the whole file.

    A per-provider snapshot's authority window reaches back to its ``discovery_started_at``
    (when its read began), which is *earlier* than its own line position (written at
    ``discovery_finished_at``). Incremental events that landed during that window -- e.g.
    an agent created mid-discovery -- sit before the snapshot line but reflect newer truth
    than the snapshot. Starting the replay at the snapshot line would skip them (a
    read-after-write gap), so instead we start at the earliest such window across all
    providers.

    Returns the offset of the first event line at or after the earliest
    ``discovery_started_at`` among every provider's latest ``DISCOVERY_PROVIDER`` snapshot
    (falling back to the latest legacy ``DISCOVERY_FULL`` timestamp only when no
    per-provider snapshot exists). Returns 0 when the file is absent or holds no snapshot
    (read the whole file). Starting a little early is harmless: per-provider snapshots
    reset only their own provider, and the per-item span rule keeps a stale snapshot from
    clobbering newer in-span events.
    """
    if not events_path.exists():
        return 0
    earliest_window_start = _find_earliest_snapshot_window_start(events_path)
    if earliest_window_start is None:
        return 0
    return _find_offset_of_first_event_at_or_after(events_path, earliest_window_start)


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

    def forget_agent(self, agent_id: str) -> None:
        """Drop a single agent from every map (it is confirmed gone)."""
        self.provider_by_agent_id.pop(agent_id, None)
        self.name_by_agent_id.pop(agent_id, None)
        self.host_id_by_agent_id.pop(agent_id, None)
        self.destroyed_agent_ids.discard(agent_id)


def _record_agent(maps: _ResolutionMaps, agent: DiscoveredAgent) -> None:
    """Record a single discovered agent into the resolution maps."""
    id_str = str(agent.agent_id)
    maps.provider_by_agent_id[id_str] = str(agent.provider_name)
    maps.name_by_agent_id[id_str] = str(agent.agent_name)
    maps.host_id_by_agent_id[id_str] = str(agent.host_id)
    maps.destroyed_agent_ids.discard(id_str)


def _apply_provider_snapshot_to_maps(
    maps: _ResolutionMaps,
    event: ProviderDiscoverySnapshotEvent,
    last_event_time_by_agent_id: Mapping[str, datetime],
) -> None:
    """Fold one per-provider snapshot into the resolution maps, span-aware.

    A per-provider snapshot is authoritative only for its own provider, and only for
    agents with no newer incremental event during its discovery span. Mirrors the
    rule in :class:`DiscoveryStateAggregator` so the resolution replay does not
    resurrect an agent destroyed mid-span, nor drop one created mid-span (the
    read-after-write case): the snapshot's read began at ``discovery_started_at``, so
    any event at/after that instant reflects newer truth than the snapshot.
    """
    span_start = event.discovery_started_at
    provider_str = str(event.provider_name)
    snapshot_agent_ids = {str(agent.agent_id) for agent in event.agents}

    # Reconcile agents previously attributed to this provider but absent from the
    # snapshot: forget them only if no newer in-span event contradicts the omission.
    prior_provider_agent_ids = [aid for aid, name in maps.provider_by_agent_id.items() if name == provider_str]
    for agent_id in prior_provider_agent_ids:
        if agent_id in snapshot_agent_ids:
            continue
        if is_intervening_event(last_event_time_by_agent_id.get(agent_id), span_start):
            continue
        maps.forget_agent(agent_id)

    # Apply each snapshot agent unless a newer in-span event already superseded it
    # (e.g. a destroy during the span must not be undone by this stale snapshot).
    for agent in event.agents:
        if is_intervening_event(last_event_time_by_agent_id.get(str(agent.agent_id)), span_start):
            continue
        _record_agent(maps, agent)


def _replay_discovery_events_into_maps(events_path: Path) -> _ResolutionMaps:
    """Replay events from the latest snapshot window into a span-aware :class:`_ResolutionMaps`.

    Starts from :func:`find_discovery_snapshot_replay_offset` -- early enough to include
    every provider's latest snapshot *and* the incremental events that landed during that
    snapshot's discovery span -- then folds events in order, applying the same span rule
    the shared aggregator uses so an in-flight snapshot never clobbers newer events.

    Raises DiscoverySchemaChangedError if any event line in the file fails schema
    validation (the caller is responsible for regenerating and retrying). Raises OSError
    on file I/O failure.
    """
    offset = find_discovery_snapshot_replay_offset(events_path)
    maps = _ResolutionMaps()
    # Most recent incremental-event time per agent, used to refuse clobbering by an
    # in-flight snapshot whose span the event falls within. Snapshots do not update it
    # (only AGENT_DISCOVERED / AGENT_DESTROYED do), matching the aggregator.
    last_event_time_by_agent_id: dict[str, datetime] = {}

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
                # Legacy global snapshot: supersedes everything before it.
                maps.reset()
                last_event_time_by_agent_id.clear()
                for agent in event.agents:
                    _record_agent(maps, agent)
            elif isinstance(event, ProviderDiscoverySnapshotEvent):
                _apply_provider_snapshot_to_maps(maps, event, last_event_time_by_agent_id)
            elif isinstance(event, AgentDiscoveryEvent):
                _record_agent(maps, event.agent)
                last_event_time_by_agent_id[str(event.agent.agent_id)] = parse_event_timestamp(event.timestamp)
            elif isinstance(event, AgentDestroyedEvent):
                maps.destroyed_agent_ids.add(str(event.agent_id))
                last_event_time_by_agent_id[str(event.agent_id)] = parse_event_timestamp(event.timestamp)
            else:
                # Host, SSH info, and error events are not relevant for resolution. A
                # host's continued existence (and its name) come from provider.get_host
                # when the caller fetches the host to stop it, so host events are skipped.
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
        # _regenerate_discovery_events uses ErrorBehavior.CONTINUE, so a
        # provider that raises during this regen still produces a fresh
        # snapshot (with the failure surfaced via error_by_provider_name).
        # Transient retries are the providers' responsibility, not this
        # layer's, so a hard failure inside list_agents itself will bubble.
        _regenerate_discovery_events(mngr_ctx)
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
    live_discovery_fallback: Callable[[Sequence[str]], Sequence[DiscoveredAgent]] | None = None,
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

    ``live_discovery_fallback`` (injected by the caller to avoid a circular import on
    the live discovery path) is consulted for any identifier the event stream cannot
    resolve -- so an agent created during an in-flight discovery span (and thus missing
    from the latest snapshot), or one created before any stream exists, still resolves.

    Raises :class:`AgentNotFoundError` if any identifier is absent from both the event
    stream and the live fallback, has been destroyed, or maps to agents on more than one
    host (which must be disambiguated with ``NAME@HOST.PROVIDER``).

    If the on-disk events are stale relative to the current model schema, this
    regenerates the snapshot once and retries, mirroring
    :func:`resolve_provider_names_for_identifiers`.
    """
    events_path = get_discovery_events_path(mngr_ctx.config)
    if not events_path.exists():
        # No stream yet (e.g. a fresh install where nothing has listed/observed): start
        # from empty maps and rely entirely on the live fallback below.
        maps = _ResolutionMaps()
    else:
        try:
            maps = _replay_discovery_events_into_maps(events_path)
        except DiscoverySchemaChangedError as e:
            logger.warning("Discovery event schema mismatch; regenerating snapshot and retrying ({})", e)
            _regenerate_discovery_events(mngr_ctx)
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

    # Read-after-write fallback: an agent created during an in-flight discovery span may
    # be absent from the latest on-disk snapshot, so the stream alone can miss it. For any
    # identifier the stream cannot resolve, ``live_discovery_fallback`` (injected by the
    # caller to avoid a circular import on the live discovery path) is consulted and its
    # agents folded in before giving up, so a just-created agent is still resolvable.
    if live_discovery_fallback is not None:
        unresolved_identifiers = [
            identifier
            for identifier in identifiers
            if identifier not in maps.provider_by_agent_id and identifier not in agent_ids_by_name
        ]
        if unresolved_identifiers:
            for agent in live_discovery_fallback(unresolved_identifiers):
                _record_agent(maps, agent)
            agent_ids_by_name = {}
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

# Baseline cadence consumers (e.g. minds) use to derive a freshness threshold for the
# discovery stream as a whole: if NO discovery event of any kind has landed in a small
# multiple of this, the pipeline has stalled (vs. a single provider being down, which
# rides along as that provider's own per-provider snapshot ``error``). This is a
# consumer-side constant; each provider's actual re-poll cadence is its configurable
# ``discovery_poll_interval_seconds``.
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


def tail_discovery_events_from_offset(
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

    Used for the synchronous attach-time read in ``tail_discovery_events_file`` so it
    shares a single warner instance with the tail thread, which lets a malformed line
    buffered at attach still surface a warning when the tail reads more data after it.

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


def tail_discovery_events_file(
    events_path: Path,
    stop_event: threading.Event,
    on_line: Callable[[str], None],
) -> None:
    """Emit the latest cached discovery snapshot(s), then tail the file for appended events.

    A pure *consumer* of an existing discovery event log: it never polls providers or
    writes snapshots. Intended for a process that observes the discovery stream
    produced by a ``mngr observe --discovery-only`` (e.g. ``mngr forward
    --observe-via-file``). Blocks until ``stop_event`` is set, so callers run it on a
    dedicated thread.

    Tolerates the file being absent when called (the tail loop waits for it to appear)
    and being truncated/rotated while tailing (it resets and re-reads). On attach it
    replays from :func:`find_discovery_snapshot_replay_offset` -- the earliest offset
    that still includes every provider's latest per-provider snapshot (or, for a pure
    pre-migration log, the legacy full snapshot) -- so a consumer attaching mid-stream
    is populated immediately
    without re-reading the whole file. The dedup set keeps a later real snapshot from
    double-emitting.
    """
    emitted_event_ids: set[str] = set()
    emit_lock = Lock()
    warner = MalformedJsonLineWarner(source_description=f"discovery events file '{events_path}'")
    if events_path.exists():
        snapshot_offset = find_discovery_snapshot_replay_offset(events_path)
        initial_offset = _emit_lines_from_offset(
            events_path, snapshot_offset, warner, emitted_event_ids, emit_lock, on_line
        )
    else:
        initial_offset = 0
    tail_discovery_events_from_offset(
        events_path, initial_offset, stop_event, emitted_event_ids, emit_lock, warner, on_line
    )


def _regenerate_discovery_events(mngr_ctx: MngrContext) -> None:
    """Run an unfiltered list to regenerate the discovery event stream (per-provider snapshots).

    The per-provider snapshots are written as a side effect of ``list_agents`` when the
    listing is unfiltered. Uses ``ErrorBehavior.CONTINUE`` so a provider that fails is
    surfaced via its own per-provider snapshot's ``error`` field rather than blocking
    emission for the other providers. The contract is that each provider is responsible
    for retrying its own transient failures before raising; no retry layer is applied
    here. Used to refresh on-disk events when a stale schema is detected during
    name/host resolution.
    """
    from imbue.mngr.api.list import list_agents

    list_agents(
        mngr_ctx=mngr_ctx,
        is_streaming=False,
        error_behavior=ErrorBehavior.CONTINUE,
        reset_caches=True,
    )
