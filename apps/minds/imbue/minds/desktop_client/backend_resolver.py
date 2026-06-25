import json
import threading
import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.desktop_client.workspace_color import normalize_workspace_color
from imbue.minds.primitives import ServiceName
from imbue.mngr.api.discovery_events import DiscoveredProvider
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo

SERVICES_EVENT_SOURCE_NAME: Final[str] = "services"
REQUESTS_EVENT_SOURCE_NAME: Final[str] = "requests"

# Every minds workspace runs a constant-named ``main``-type agent whose
# bootstrap execs supervisord (and thus owns the system interface). This is the
# canonical definition of that name; ``agent_creator._DEFAULT_AGENT_NAME``
# is the ``AgentName``-typed form built from it.
SYSTEM_SERVICES_AGENT_NAME: Final[str] = "system-services"


class AgentDisplayInfo(FrozenModel):
    """Display-oriented information about an agent for UI rendering."""

    agent_name: str = Field(description="Human-readable agent name")
    host_id: str = Field(description="Host identifier (e.g. 'localhost' or a remote host ID)")
    create_time: datetime | None = Field(
        default=None, description="When the agent was created (UTC), if known from discovery"
    )
    provider_name: str | None = Field(
        default=None, description="Provider instance the agent's host runs on, if known from discovery"
    )


class ServiceLogParseError(ValueError):
    """Raised when a service log record cannot be parsed."""


class ServiceLogRecord(FrozenModel):
    """A record of a service started by an agent, as written to services/events.jsonl.

    Each line of services/events.jsonl is a JSON object with these fields.
    Agents write these records on startup so the desktop client can discover them.
    """

    service: ServiceName = Field(description="Name of the service (e.g., 'web')")
    url: str = Field(description="URL where the service is accessible (e.g., 'http://127.0.0.1:9100')")


class BackendResolverInterface(MutableModel, ABC):
    """Resolves agent IDs and service names to their backend service URLs.

    Each agent may run multiple services (e.g. 'web', 'api'), each accessible
    at a different URL. The resolver maps (agent_id, service_name) pairs to URLs.
    """

    @abstractmethod
    def get_backend_url(self, agent_id: AgentId, service_name: ServiceName) -> str | None:
        """Return the backend URL for a specific service of an agent, or None if unknown/offline."""

    @abstractmethod
    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        """Return all known agent IDs."""

    def list_known_workspace_ids(self) -> tuple[AgentId, ...]:
        """Return agent IDs that have the workspace=true label.

        Default implementation returns all known agent IDs (no filtering).
        Subclasses with access to agent labels should override this.

        This is the *full* set, including workspaces whose host has been
        destroyed (retained for the provider's destroyed-host persistence
        window). Active-workspace surfaces should call
        :meth:`list_active_workspace_ids` instead; a restore view that needs
        the destroyed ones uses this plus :meth:`get_host_state`.
        """
        return self.list_known_agent_ids()

    def list_active_workspace_ids(self) -> tuple[AgentId, ...]:
        """Return workspace agent IDs whose host is not in a terminal DESTROYED state.

        Default implementation has no host-state data, so it returns the same
        set as :meth:`list_known_workspace_ids`. Subclasses with discovery host
        state should override to drop agents on DESTROYED hosts.
        """
        return self.list_known_workspace_ids()

    def get_host_state(self, host_id: HostId) -> HostState | None:
        """Return the last-known lifecycle state of a host, or None if unknown.

        Default implementation has no host-state data and returns None.
        Subclasses fed by discovery should override this. Implementations may
        return a short-lived optimistic override set via
        :meth:`set_host_state_override` ahead of discovery catching up.
        """
        return None

    def set_host_state_override(self, host_id: HostId, state: HostState) -> None:
        """Optimistically override a host's state until discovery confirms it.

        Lets a UI-initiated lifecycle action (e.g. a Start/Stop click) flip
        :meth:`get_host_state` immediately instead of waiting for the next
        discovery snapshot. The override is dropped once discovery agrees with
        it or a short TTL elapses. Default implementation is a no-op (resolvers
        without discovery host state have nothing to override).
        """

    def clear_host_state_override(self, host_id: HostId) -> None:
        """Drop any optimistic override for ``host_id`` (e.g. after a failed action).

        Default implementation is a no-op.
        """

    @abstractmethod
    def list_services_for_agent(self, agent_id: AgentId) -> tuple[ServiceName, ...]:
        """Return all known service names for an agent, sorted alphabetically."""

    def get_ssh_info(self, agent_id: AgentId) -> RemoteSSHInfo | None:
        """Return SSH connection info for the agent's host, or None for local agents.

        Default implementation returns None (all agents treated as local).
        Subclasses that discover remote agents should override this.
        """
        return None

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        """Return display-oriented info about an agent, or None if unknown.

        Default implementation returns a minimal result using the agent_id as the name.
        Subclasses with richer agent data should override this.
        """
        if agent_id in self.list_known_agent_ids():
            return AgentDisplayInfo(agent_name=str(agent_id), host_id="localhost")
        return None

    def get_workspace_name(self, agent_id: AgentId) -> str | None:
        """Return the workspace label value for an agent, or None.

        Default implementation returns None.
        Subclasses with access to agent labels should override this.
        """
        return None

    def get_workspace_color(self, agent_id: AgentId) -> str | None:
        """Return the workspace color hex for an agent, or None if unset.

        Returns a normalized ``#rrggbb`` lowercase string, ``None`` if the
        agent has no ``color`` label (callers fall back to the default
        workspace color), or the default color hex if the stored label is
        malformed.

        Default implementation returns None. Subclasses with access to
        agent labels should override this.
        """
        return None

    def get_system_services_agent_id(self, workspace_agent_id: AgentId) -> AgentId | None:
        """Return the ``system-services`` agent id that shares the workspace agent's host.

        Default implementation returns None.
        Subclasses with access to per-host agent data should override this.
        """
        return None

    def has_completed_initial_discovery(self) -> bool:
        """Whether any agent discovery data has been received.

        Before this returns True, the agent list may be incomplete. The landing
        page uses this to distinguish "still discovering" from "no agents exist."
        Default implementation returns True (appropriate for static resolvers).
        """
        return True

    def get_provider_errors(self) -> dict[ProviderInstanceName, DiscoveryError]:
        """Return errored providers keyed by name from the latest discovery snapshot.

        Default implementation returns an empty mapping (resolvers without
        provider state never report errors); ``MngrCliBackendResolver``
        overrides it. The workspace list uses this to mark a retained-but-
        unverified workspace stale when its provider's last poll errored.
        """
        return {}

    def get_freshness_timestamps(self) -> tuple[datetime | None, datetime | None]:
        """Return ``(last_event_at, last_full_snapshot_at)`` from discovery.

        Default implementation returns ``(None, None)`` (resolvers without
        discovery have no freshness to report); ``MngrCliBackendResolver``
        overrides it. ``None`` for the last full snapshot means discovery has
        not (recently) confirmed state, so callers that gate on freshness treat
        it as stale.
        """
        return None, None


class StaticBackendResolver(BackendResolverInterface):
    """Resolves backend URLs from a static mapping provided at construction time.

    The mapping is structured as {agent_id: {service_name: url}}.
    """

    url_by_agent_and_service: Mapping[str, Mapping[str, str]] = Field(
        frozen=True,
        description="Mapping of agent ID to mapping of service name to backend URL",
    )
    ssh_info_by_agent_id: Mapping[str, RemoteSSHInfo] = Field(
        default_factory=dict,
        frozen=True,
        description="Optional SSH info keyed by agent ID string, for static/remote agents.",
    )

    def get_backend_url(self, agent_id: AgentId, service_name: ServiceName) -> str | None:
        services = self.url_by_agent_and_service.get(str(agent_id))
        if services is None:
            return None
        return services.get(str(service_name))

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        return tuple(AgentId(agent_id) for agent_id in sorted(self.url_by_agent_and_service.keys()))

    def list_services_for_agent(self, agent_id: AgentId) -> tuple[ServiceName, ...]:
        services = self.url_by_agent_and_service.get(str(agent_id))
        if services is None:
            return ()
        return tuple(ServiceName(name) for name in sorted(services.keys()))

    def get_ssh_info(self, agent_id: AgentId) -> RemoteSSHInfo | None:
        return self.ssh_info_by_agent_id.get(str(agent_id))


# -- Parsing helpers --


class ParsedAgentsResult(FrozenModel):
    """Result of parsing agent and SSH info from discovery events or mngr list --format json output."""

    agent_ids: tuple[AgentId, ...] = Field(default=(), description="All discovered agent IDs")
    discovered_agents: tuple[DiscoveredAgent, ...] = Field(
        default=(), description="Full DiscoveredAgent data for each agent"
    )
    ssh_info_by_agent_id: Mapping[str, RemoteSSHInfo] = Field(
        default_factory=dict,
        description="SSH info keyed by agent ID string, only for remote agents",
    )
    host_state_by_host_id: Mapping[str, HostState] = Field(
        default_factory=dict,
        description="Host lifecycle state keyed by host ID string, for hosts whose state is known",
    )


def parse_agents_from_json(json_output: str | None) -> ParsedAgentsResult:
    """Parse agent IDs and SSH info from mngr list --format json output.

    Returns both agent IDs and a mapping of agent ID -> RemoteSSHInfo for agents
    that have SSH connection info (i.e., are running on remote hosts).
    """
    if json_output is None:
        return ParsedAgentsResult()
    try:
        data = json.loads(json_output)
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse mngr list output: {}", e)
        return ParsedAgentsResult()

    agents = data.get("agents", [])
    agent_ids: list[AgentId] = []
    ssh_info_by_id: dict[str, RemoteSSHInfo] = {}
    host_state_by_host_id: dict[str, HostState] = {}

    for agent in agents:
        agent_id_str = agent.get("id")
        if agent_id_str is None:
            continue
        agent_ids.append(AgentId(agent_id_str))

        host = agent.get("host")
        if host is None:
            continue

        host_id_value = host.get("id")
        state_value = host.get("state")
        if isinstance(host_id_value, str) and isinstance(state_value, str):
            try:
                host_state_by_host_id[host_id_value] = HostState(state_value)
            except ValueError:
                logger.warning("Unknown host state {!r} for host {}", state_value, host_id_value)

        ssh = host.get("ssh")
        if ssh is None:
            continue

        try:
            ssh_info = RemoteSSHInfo(
                user=ssh["user"],
                host=ssh["host"],
                port=ssh["port"],
                key_path=Path(ssh["key_path"]),
            )
            ssh_info_by_id[agent_id_str] = ssh_info
        except (KeyError, ValueError) as e:
            logger.warning("Failed to parse SSH info for agent {}: {}", agent_id_str, e)

    return ParsedAgentsResult(
        agent_ids=tuple(agent_ids),
        ssh_info_by_agent_id=ssh_info_by_id,
        host_state_by_host_id=host_state_by_host_id,
    )


def parse_agent_ids_from_json(json_output: str | None) -> tuple[AgentId, ...]:
    """Parse agent IDs from mngr list --format json output, discarding SSH info."""
    return parse_agents_from_json(json_output).agent_ids


class ServiceDeregisteredRecord(FrozenModel):
    """A record of a service being deregistered by an agent.

    Written to services/events.jsonl when an application is removed.
    """

    service: ServiceName = Field(description="Name of the service being deregistered")


def parse_service_log_record(raw: dict[str, object]) -> ServiceLogRecord | ServiceDeregisteredRecord:
    """Parse a single JSON dict into a ServiceLogRecord or ServiceDeregisteredRecord.

    Extracts the 'service' field and checks the 'type' field.
    For 'service_deregistered' events, returns a ServiceDeregisteredRecord.
    For all other events, returns a ServiceLogRecord with 'service' and 'url'.
    Raises ValueError if required fields are missing.
    """
    event_type = raw.get("type", "service_registered")
    service = raw.get("service")

    if not service:
        raise ServiceLogParseError("Service log record missing 'service' field")

    if event_type == "service_deregistered":
        return ServiceDeregisteredRecord(service=ServiceName(str(service)))

    url = raw.get("url")
    if not url:
        raise ServiceLogParseError(f"Service log record missing required fields (service={service!r}, url={url!r})")
    return ServiceLogRecord(service=ServiceName(str(service)), url=str(url))


def parse_service_log_records(text: str) -> list[ServiceLogRecord | ServiceDeregisteredRecord]:
    """Parse JSONL text into service log records (registered or deregistered).

    Uses the 'type' field to distinguish registered from deregistered events.
    Registered events require 'service' and 'url'; deregistered events require
    only 'service'. Other envelope fields (timestamp, event_id, source) are ignored.
    Raises on malformed lines rather than silently skipping them.
    """
    records: list[ServiceLogRecord | ServiceDeregisteredRecord] = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        raw = json.loads(line)
        records.append(parse_service_log_record(raw))
    return records


# -- Last-good agent topology (system-services fallback) --


class _AgentRecord(FrozenModel):
    """The minimal agent-topology fields needed to resolve the system-services agent.

    A trimmed projection of ``DiscoveredAgent`` (id, host, name) -- the only
    fields ``get_system_services_agent_id`` consults. Kept small so the
    persisted last-good snapshot carries no more than the fallback needs.
    """

    agent_id: AgentId = Field(description="The agent's id")
    host_id: HostId = Field(description="The id of the host the agent runs on")
    agent_name: AgentName = Field(description="The agent's name (the system-services agent is constant-named)")


class _LastGoodAgentTopology(FrozenModel):
    """The most recent *complete* per-host agent topology, persisted to disk.

    Maps host id -> the agents discovered on that host, recorded only for
    hosts whose snapshot included the system-services agent (a complete
    enumeration). Hosts whose enumeration was incomplete -- or that dropped
    out of discovery entirely (the SSH-dead failure mode) -- retain their
    last complete record rather than being clobbered by a partial view. This
    is what lets the system-services fallback survive a discovery loss.
    """

    agents_by_host: Mapping[str, tuple[_AgentRecord, ...]] = Field(
        default_factory=dict, description="Host id -> the agents last seen on that host with a complete enumeration"
    )


def _to_agent_record(agent: DiscoveredAgent) -> _AgentRecord:
    """Trim a ``DiscoveredAgent`` down to the topology fields the fallback needs."""
    return _AgentRecord(agent_id=agent.agent_id, host_id=agent.host_id, agent_name=agent.agent_name)


def _find_system_services_agent(records: Iterable[_AgentRecord], workspace_agent_id: AgentId) -> AgentId | None:
    """Resolve the system-services agent that shares the workspace agent's host.

    The single lookup behind both the live-snapshot and last-good-fallback
    paths: locate the workspace agent to learn its host, then return the
    system-services agent on that same host. ``None`` if either is absent.
    """
    records = tuple(records)
    host_id: HostId | None = next(
        (record.host_id for record in records if record.agent_id == workspace_agent_id), None
    )
    if host_id is None:
        return None
    for record in records:
        if record.host_id == host_id and str(record.agent_name) == SYSTEM_SERVICES_AGENT_NAME:
            return record.agent_id
    return None


def _read_last_good_agent_topology(path: Path) -> _LastGoodAgentTopology:
    """Read the persisted last-good topology from ``path``.

    Returns an empty topology for a missing file, a malformed file, or any
    content that fails validation; we never want a corrupt cache to break
    minds startup. The next complete discovery snapshot rewrites the file.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _LastGoodAgentTopology()
    except OSError as exc:
        logger.warning("Could not read last-good agent topology from {}: {}", path, exc)
        return _LastGoodAgentTopology()
    try:
        return _LastGoodAgentTopology.model_validate_json(raw)
    except ValueError as exc:
        logger.warning("Last-good agent topology at {} is not valid: {}", path, exc)
        return _LastGoodAgentTopology()


def _write_last_good_agent_topology(path: Path, topology: _LastGoodAgentTopology) -> None:
    """Persist the last-good topology atomically to ``path``.

    Writes to a sibling ``.tmp`` file then renames to defend against a crash
    mid-write leaving a truncated file. A write failure logs a warning but
    does not propagate -- the topology is a best-effort fallback, and crashing
    the discovery thread over a transient I/O error would be worse than
    losing one update.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(topology.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("Could not write last-good agent topology to {}: {}", path, exc)


# -- MngrCliBackendResolver --


# How long an optimistic host-state override is trusted before discovery is
# believed instead. A UI lifecycle action's command has already returned by the
# time the override is set, so the next discovery snapshot (~10s) normally
# confirms it well within this window; the TTL only bounds how long a *stuck*
# discovery (e.g. a provider erroring) can keep showing a stale optimistic state.
_HOST_STATE_OVERRIDE_TTL_SECONDS: Final[float] = 90.0


class _HostStateOverride(FrozenModel):
    """A short-lived optimistic host state set by a UI-initiated lifecycle action."""

    state: HostState = Field(description="The optimistic state to report until discovery confirms it")
    set_at_monotonic: float = Field(description="time.monotonic() when the override was set, for TTL expiry")


class MngrCliBackendResolver(BackendResolverInterface):
    """Resolves backend URLs from continuously-updated state.

    State is updated externally via update_agents() and update_services() methods.
    In production, a MngrStreamManager calls these methods from background threads
    that stream data from `mngr observe --discovery-only` and `mngr event --follow`.

    All reads are thread-safe via an internal lock.
    """

    last_good_agents_path: Path | None = Field(
        default=None,
        description=(
            "Optional JSON file recording the last-good per-host agent topology. "
            "Updated as discovery completely enumerates a host (the system-services "
            "agent is present); consulted by ``get_system_services_agent_id`` when "
            "live discovery has dropped the host (the SSH-dead failure mode). When "
            "None, the topology is in-memory only."
        ),
    )

    _agents_result: ParsedAgentsResult = PrivateAttr(default_factory=ParsedAgentsResult)
    _services_by_agent: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    _initial_discovery_done: bool = PrivateAttr(default=False)
    _providers: tuple[DiscoveredProvider, ...] = PrivateAttr(default=())
    _error_by_provider_name: dict[ProviderInstanceName, DiscoveryError] = PrivateAttr(default_factory=dict)
    # Timestamp (UTC) of the most recently received discovery event of any kind.
    # Used by the providers panel's "time since last discovery event" counter.
    _last_event_at: datetime | None = PrivateAttr(default=None)
    # Timestamp (UTC) of the most recently received FullDiscoverySnapshotEvent.
    # Used by the providers panel's "time since last full discovery event" counter.
    _last_full_snapshot_at: datetime | None = PrivateAttr(default=None)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _on_change_callbacks: list[Callable[[], None]] = PrivateAttr(default_factory=list)
    _on_request_callbacks: list[Callable[[str, str], None]] = PrivateAttr(default_factory=list)
    # host_id_str -> a short-lived optimistic state set by a UI lifecycle action,
    # masking discovery in ``get_host_state`` until discovery agrees or the TTL
    # elapses. Guarded by _lock. Only ever holds a real RUNNING/STOPPED-style
    # transition the user just triggered -- never DESTROYED -- so it cannot affect
    # the DESTROYED-only filtering in ``list_active_workspace_ids``.
    _host_state_override_by_host_id: dict[str, _HostStateOverride] = PrivateAttr(default_factory=dict)
    # host_id_str -> the agents last completely enumerated on that host (the
    # in-memory image of the persisted last-good topology). Updated under
    # _lock by update_agents; read by get_system_services_agent_id as the
    # fallback when live discovery has lost the host.
    _last_good_agents_by_host: dict[str, tuple[_AgentRecord, ...]] = PrivateAttr(default_factory=dict)
    # Set of agent ids for which we've already logged a malformed-color-label
    # warning, so the log line fires once per agent rather than on every SSE
    # tick. Plain set is fine -- get_workspace_color holds ``_lock`` while
    # mutating it.
    _logged_malformed_color_agents: set[str] = PrivateAttr(default_factory=set)

    def model_post_init(self, __context: object) -> None:
        """Load the persisted last-good agent topology from disk, if configured."""
        if self.last_good_agents_path is not None:
            self._last_good_agents_by_host = dict(
                _read_last_good_agent_topology(self.last_good_agents_path).agents_by_host
            )

    def add_on_change_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked whenever agent or service data changes.

        Callbacks are invoked synchronously from the thread that made the change
        (typically a MngrStreamManager background thread). Keep callbacks fast
        and non-blocking -- they should just signal an event, not do real work.

        Call remove_on_change_callback() with the same callable to unregister it.
        """
        with self._lock:
            self._on_change_callbacks.append(callback)

    def remove_on_change_callback(self, callback: Callable[[], None]) -> None:
        """Unregister a previously registered change callback.

        Safe to call even if the callback is not currently registered (no-op).
        """
        with self._lock:
            try:
                self._on_change_callbacks.remove(callback)
            except ValueError:
                pass

    def _fire_on_change(self) -> None:
        """Invoke all registered change callbacks.

        Takes a snapshot of the callbacks list under the lock, then calls each
        callback outside the lock to avoid holding the lock during potentially
        blocking operations.
        """
        with self._lock:
            callbacks = list(self._on_change_callbacks)
        for callback in callbacks:
            try:
                callback()
            except (OSError, RuntimeError) as e:
                logger.warning("Resolver change callback failed: {}", e)

    def notify_change(self) -> None:
        """Public wake-up for SSE listeners after external state mutations.

        ``_fire_on_change`` is fired internally on agent/service updates, but
        the request inbox lives outside this resolver. Inbox mutations
        (new request events, mirrored response events) call this so chrome
        SSE consumers don't have to wait for the next 30s poll tick.
        """
        self._fire_on_change()

    def update_agents(self, result: ParsedAgentsResult) -> None:
        """Replace the known agent list and SSH info. Thread-safe.

        Also folds the new agent list into the last-good per-host topology:
        each host whose snapshot includes the ``system-services`` agent (a
        complete enumeration) has its record refreshed and written through to
        the on-disk topology (if configured). Hosts with an incomplete view --
        or absent from the snapshot entirely (transient discovery loss) --
        keep their last complete record; that is the entire point of the
        topology.
        """
        path = self.last_good_agents_path
        topology_to_write: _LastGoodAgentTopology | None = None
        with self._lock:
            self._agents_result = result
            self._initial_discovery_done = True
            self._sweep_host_state_overrides_locked(result.host_state_by_host_id)
            if self._merge_last_good_topology_locked(result.discovered_agents) and path is not None:
                topology_to_write = _LastGoodAgentTopology(agents_by_host=dict(self._last_good_agents_by_host))
        if path is not None and topology_to_write is not None:
            _write_last_good_agent_topology(path, topology_to_write)
        self._fire_on_change()

    def _sweep_host_state_overrides_locked(self, discovery_state_by_host_id: Mapping[str, HostState]) -> None:
        """Drop optimistic overrides that the fresh snapshot has confirmed or that have expired.

        Keeps the override map bounded to genuinely-still-pending overrides (so a
        host that has since left discovery never lingers) without firing on-change
        itself -- the surrounding ``update_agents`` already fires once. Must be
        called with ``self._lock`` held.
        """
        now = time.monotonic()
        for host_id_str in tuple(self._host_state_override_by_host_id):
            override = self._host_state_override_by_host_id[host_id_str]
            discovery_state = discovery_state_by_host_id.get(host_id_str)
            if (
                discovery_state == override.state
                or (now - override.set_at_monotonic) > _HOST_STATE_OVERRIDE_TTL_SECONDS
            ):
                del self._host_state_override_by_host_id[host_id_str]

    def _merge_last_good_topology_locked(self, agents: tuple[DiscoveredAgent, ...]) -> bool:
        """Fold a fresh discovery snapshot into the last-good per-host topology.

        Only hosts whose snapshot includes the system-services agent are
        treated as completely enumerated and overwrite their prior record;
        hosts with an incomplete view (or absent from the snapshot) keep their
        last complete record. Must be called with ``self._lock`` held.

        Returns True if any host record changed (so the caller persists).
        """
        agents_by_host: dict[str, list[DiscoveredAgent]] = {}
        for agent in agents:
            agents_by_host.setdefault(str(agent.host_id), []).append(agent)
        changed = False
        for host_id_str, host_agents in agents_by_host.items():
            if not any(str(agent.agent_name) == SYSTEM_SERVICES_AGENT_NAME for agent in host_agents):
                continue
            new_records = tuple(_to_agent_record(agent) for agent in host_agents)
            if self._last_good_agents_by_host.get(host_id_str) != new_records:
                self._last_good_agents_by_host[host_id_str] = new_records
                changed = True
        return changed

    def update_providers(
        self,
        providers: tuple[DiscoveredProvider, ...],
        error_by_provider_name: Mapping[ProviderInstanceName, DiscoveryError],
        last_full_snapshot_at: datetime,
    ) -> None:
        """Replace provider state from a FullDiscoverySnapshotEvent. Thread-safe.

        Updates both ``_last_event_at`` and ``_last_full_snapshot_at`` to
        ``last_full_snapshot_at`` since a full snapshot is also a discovery
        event. Incremental events update ``_last_event_at`` only, via
        :meth:`record_discovery_event_received`.
        """
        with self._lock:
            self._providers = tuple(providers)
            self._error_by_provider_name = dict(error_by_provider_name)
            self._last_full_snapshot_at = last_full_snapshot_at
            self._last_event_at = last_full_snapshot_at
        self._fire_on_change()

    def record_discovery_event_received(self, event_at: datetime) -> None:
        """Bump ``_last_event_at`` for an incremental (non-snapshot) discovery event."""
        with self._lock:
            self._last_event_at = event_at
        self._fire_on_change()

    def list_providers(self) -> tuple[DiscoveredProvider, ...]:
        """Return the providers from the most recent full discovery snapshot."""
        with self._lock:
            return self._providers

    def get_provider_errors(self) -> dict[ProviderInstanceName, DiscoveryError]:
        """Return errored providers keyed by provider name."""
        with self._lock:
            return dict(self._error_by_provider_name)

    def get_freshness_timestamps(self) -> tuple[datetime | None, datetime | None]:
        """Return ``(last_event_at, last_full_snapshot_at)`` for the providers panel."""
        with self._lock:
            return self._last_event_at, self._last_full_snapshot_at

    def update_services(self, agent_id: AgentId, services: dict[str, str]) -> None:
        """Replace the known services for a single agent. Thread-safe."""
        with self._lock:
            self._services_by_agent[str(agent_id)] = services
        self._fire_on_change()

    def get_backend_url(self, agent_id: AgentId, service_name: ServiceName) -> str | None:
        with self._lock:
            services = self._services_by_agent.get(str(agent_id), {})
            return services.get(str(service_name))

    def list_services_for_agent(self, agent_id: AgentId) -> tuple[ServiceName, ...]:
        with self._lock:
            services = self._services_by_agent.get(str(agent_id), {})
            return tuple(ServiceName(name) for name in sorted(services.keys()))

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        with self._lock:
            return self._agents_result.agent_ids

    def list_discovered_agents(self) -> tuple[DiscoveredAgent, ...]:
        """Return the full ``DiscoveredAgent`` records from the latest discovery snapshot.

        Unlike :meth:`list_known_agent_ids`, this exposes the typed
        ``host_id`` / ``agent_name`` / ``labels`` alongside each id so
        callers that need to act on agent-host pairs (e.g. the latchkey
        auto-register callback) do not have to do N+1 lookups via
        :meth:`get_agent_display_info` and re-parse stringified ids.
        """
        with self._lock:
            return self._agents_result.discovered_agents

    def list_known_workspace_ids(self) -> tuple[AgentId, ...]:
        """Return agent IDs that are primary workspace agents.

        Filters for agents with both ``workspace`` and ``is_primary`` labels.
        Includes workspaces on DESTROYED hosts; see the interface docstring.
        """
        with self._lock:
            return tuple(
                agent.agent_id
                for agent in self._agents_result.discovered_agents
                if "workspace" in agent.labels and "is_primary" in agent.labels
            )

    def list_active_workspace_ids(self) -> tuple[AgentId, ...]:
        """Return primary workspace agent IDs whose host is not DESTROYED.

        A destroyed host lingers in discovery for the provider's destroyed-host
        persistence window; its workspace agents stay in the snapshot but should
        drop off every active surface. Filtering here (rather than removing the
        agents from the snapshot) keeps the full set available via
        :meth:`list_known_workspace_ids` for a future restore view.
        """
        with self._lock:
            host_state_by_host_id = self._agents_result.host_state_by_host_id
            return tuple(
                agent.agent_id
                for agent in self._agents_result.discovered_agents
                if "workspace" in agent.labels
                and "is_primary" in agent.labels
                and host_state_by_host_id.get(str(agent.host_id)) is not HostState.DESTROYED
            )

    def get_host_state(self, host_id: HostId) -> HostState | None:
        """Return the host's lifecycle state, preferring a fresh optimistic override.

        Discovery is authoritative: an optimistic override (set by a UI-initiated
        Start/Stop) wins only until discovery agrees with it or its TTL elapses, at
        which point it is dropped here and discovery is returned. Returns None when
        neither an override nor discovery knows the host.
        """
        host_id_str = str(host_id)
        with self._lock:
            discovery_state = self._agents_result.host_state_by_host_id.get(host_id_str)
            override = self._host_state_override_by_host_id.get(host_id_str)
            if override is None:
                return discovery_state
            if (
                discovery_state == override.state
                or (time.monotonic() - override.set_at_monotonic) > _HOST_STATE_OVERRIDE_TTL_SECONDS
            ):
                del self._host_state_override_by_host_id[host_id_str]
                return discovery_state
            return override.state

    def set_host_state_override(self, host_id: HostId, state: HostState) -> None:
        """Optimistically override ``host_id``'s state until discovery confirms it; fires on-change."""
        with self._lock:
            self._host_state_override_by_host_id[str(host_id)] = _HostStateOverride(
                state=state, set_at_monotonic=time.monotonic()
            )
        self._fire_on_change()

    def clear_host_state_override(self, host_id: HostId) -> None:
        """Drop any optimistic override for ``host_id``; fires on-change only if one was present."""
        with self._lock:
            existed = self._host_state_override_by_host_id.pop(str(host_id), None) is not None
        if existed:
            self._fire_on_change()

    def get_workspace_name(self, agent_id: AgentId) -> str | None:
        """Return the workspace label value for an agent, or None."""
        with self._lock:
            for agent in self._agents_result.discovered_agents:
                if agent.agent_id == agent_id:
                    return agent.labels.get("workspace")
            return None

    def get_workspace_color(self, agent_id: AgentId) -> str | None:
        """Return the normalized ``#rrggbb`` color label for an agent.

        Returns ``None`` when the agent has no ``color`` label (callers
        fall back to the default workspace color). Defensively parses the stored
        value: if it is non-empty but not a recognized hex literal, logs
        once at WARNING and returns the default workspace color so the
        UI never crashes on a bad label. Mngr itself does not validate
        label values, so a hand-edited or future-version label might
        carry junk.
        """
        with self._lock:
            for agent in self._agents_result.discovered_agents:
                if agent.agent_id == agent_id:
                    raw = agent.labels.get("color")
                    if raw is None:
                        return None
                    normalized = normalize_workspace_color(raw)
                    if normalized is None:
                        if str(agent_id) not in self._logged_malformed_color_agents:
                            logger.warning(
                                "Ignoring malformed color label {!r} for agent {}; "
                                "rendering as default. Repick in workspace settings to fix.",
                                raw,
                                agent_id,
                            )
                            self._logged_malformed_color_agents.add(str(agent_id))
                        return DEFAULT_WORKSPACE_COLOR
                    return normalized
            return None

    def set_workspace_color_locally(self, agent_id: AgentId, color_hex: str) -> bool:
        """Optimistically update the cached ``color`` label for an agent.

        Called by the settings POST handler after a successful ``mngr label``
        write so the SSE workspaces payload reflects the new color on the
        next emit -- without having to wait the ~10s discovery tick for
        the change to propagate back through ``mngr observe``.

        ``color_hex`` must already be normalized (``#rrggbb`` lowercase);
        the caller is responsible for validation. Returns True if the
        snapshot was updated and ``_fire_on_change`` was called, False
        if the agent is not in the current snapshot (in which case the
        next discovery emit will pick up the on-disk label anyway).
        """
        with self._lock:
            updated_agents: list[DiscoveredAgent] = []
            found = False
            for agent in self._agents_result.discovered_agents:
                if agent.agent_id == agent_id:
                    found = True
                    new_labels = {**agent.labels, "color": color_hex}
                    new_certified_data = {**agent.certified_data, "labels": new_labels}
                    updated_agents.append(
                        agent.model_copy_update(to_update(agent.field_ref().certified_data, new_certified_data))
                    )
                else:
                    updated_agents.append(agent)
            if not found:
                return False
            self._agents_result = self._agents_result.model_copy_update(
                to_update(self._agents_result.field_ref().discovered_agents, tuple(updated_agents))
            )
            self._logged_malformed_color_agents.discard(str(agent_id))
        self._fire_on_change()
        return True

    def get_ssh_info(self, agent_id: AgentId) -> RemoteSSHInfo | None:
        """Return SSH info for the agent's host, or None for local agents."""
        with self._lock:
            return self._agents_result.ssh_info_by_agent_id.get(str(agent_id))

    def get_system_services_agent_id(self, workspace_agent_id: AgentId) -> AgentId | None:
        """Return the ``system-services`` agent sharing the workspace agent's host.

        The workspace (claude) agent and the system-services agent run in the
        same container, so they share a host id. The lookup runs the same
        host-and-name search over the current discovery snapshot first; if that
        snapshot does not contain the host, it runs the identical search over
        the persisted last-good topology so a restart can still address the
        system-services agent.
        """
        with self._lock:
            live = _find_system_services_agent(
                (_to_agent_record(agent) for agent in self._agents_result.discovered_agents),
                workspace_agent_id,
            )
            if live is not None:
                return live
            last_good = (record for records in self._last_good_agents_by_host.values() for record in records)
            return _find_system_services_agent(last_good, workspace_agent_id)

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        """Return display info from discovered agent data."""
        with self._lock:
            for agent in self._agents_result.discovered_agents:
                if agent.agent_id == agent_id:
                    return AgentDisplayInfo(
                        agent_name=str(agent.agent_name),
                        host_id=str(agent.host_id),
                        create_time=agent.create_time,
                        provider_name=str(agent.provider_name),
                    )
            return None

    def has_completed_initial_discovery(self) -> bool:
        with self._lock:
            return self._initial_discovery_done

    def add_on_request_callback(self, callback: Callable[[str, str], None]) -> None:
        """Register a callback invoked when a request event arrives.

        The callback receives (agent_id_str, raw_json_line).
        """
        with self._lock:
            self._on_request_callbacks.append(callback)

    def remove_on_request_callback(self, callback: Callable[[str, str], None]) -> None:
        """Unregister a request event callback."""
        with self._lock:
            try:
                self._on_request_callbacks.remove(callback)
            except ValueError:
                pass

    def fire_on_request(self, agent_id_str: str, raw_line: str) -> None:
        """Invoke all registered request event callbacks.

        Public dispatch entry point used by both the legacy in-process
        ``MngrStreamManager`` and the new ``EnvelopeStreamConsumer``.
        """
        with self._lock:
            callbacks = list(self._on_request_callbacks)
        for callback in callbacks:
            try:
                callback(agent_id_str, raw_line)
            except (OSError, RuntimeError) as e:
                logger.warning("Request event callback failed: {}", e)

    def _fire_on_request(self, agent_id_str: str, raw_line: str) -> None:
        """Internal alias for ``fire_on_request`` retained for backward compatibility."""
        self.fire_on_request(agent_id_str, raw_line)
