import json
import threading
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.primitives import ServiceName
from imbue.mngr.api.discovery_events import DiscoveredProvider
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo

SERVICES_EVENT_SOURCE_NAME: Final[str] = "services"
REQUESTS_EVENT_SOURCE_NAME: Final[str] = "requests"
REFRESH_EVENT_SOURCE_NAME: Final[str] = "refresh"

# Every minds workspace runs a constant-named ``main``-type agent that owns
# the bootstrap service manager (and thus the system interface). This is the
# canonical definition of that name; ``agent_creator._DEFAULT_AGENT_NAME``
# is the ``AgentName``-typed form built from it.
SYSTEM_SERVICES_AGENT_NAME: Final[str] = "system-services"


class AgentDisplayInfo(FrozenModel):
    """Display-oriented information about an agent for UI rendering."""

    agent_name: str = Field(description="Human-readable agent name")
    host_id: str = Field(description="Host identifier (e.g. 'localhost' or a remote host ID)")


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
        """
        return self.list_known_agent_ids()

    def list_known_or_cached_workspace_ids(self) -> tuple[AgentId, ...]:
        """Return workspace agent ids including any persisted cache entries.

        Default implementation returns the live-only list. Subclasses that
        maintain a workspace cache should override this so the landing page
        and chrome workspaces list keep rendering tiles for workspaces whose
        live discovery has transiently dropped them (e.g. SSH-dead docker).
        """
        return self.list_known_workspace_ids()

    def evict_cached_workspace(self, agent_id: AgentId) -> None:
        """Drop ``agent_id`` from any persisted workspace cache, if applicable.

        Default implementation is a no-op. Subclasses that maintain a
        workspace cache should override this; callers use it to prune a
        cache entry once minds has confirmed the workspace was destroyed.
        """
        return None

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


class StaticBackendResolver(BackendResolverInterface):
    """Resolves backend URLs from a static mapping provided at construction time.

    The mapping is structured as {agent_id: {service_name: url}}.
    """

    url_by_agent_and_service: Mapping[str, Mapping[str, str]] = Field(
        frozen=True,
        description="Mapping of agent ID to mapping of service name to backend URL",
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

    for agent in agents:
        agent_id_str = agent.get("id")
        if agent_id_str is None:
            continue
        agent_ids.append(AgentId(agent_id_str))

        host = agent.get("host")
        if host is None:
            continue
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


# -- Services-agent-id cache helpers --


def _services_pairs_from_agents(agents: tuple[DiscoveredAgent, ...]) -> dict[str, str]:
    """Map each workspace agent id to the system-services agent id on its host.

    Skips hosts that contain only one of the two. Returns ``{}`` when no pair
    can be observed in the current discovery snapshot.
    """
    services_by_host: dict[str, str] = {}
    workspaces_by_host: dict[str, list[str]] = {}
    for agent in agents:
        host_id_str = str(agent.host_id)
        if str(agent.agent_name) == SYSTEM_SERVICES_AGENT_NAME:
            services_by_host[host_id_str] = str(agent.agent_id)
        else:
            workspaces_by_host.setdefault(host_id_str, []).append(str(agent.agent_id))
    pairs: dict[str, str] = {}
    for host_id_str, workspace_ids in workspaces_by_host.items():
        services_id = services_by_host.get(host_id_str)
        if services_id is None:
            continue
        for workspace_id in workspace_ids:
            pairs[workspace_id] = services_id
    return pairs


def _read_services_agent_id_cache(path: Path) -> dict[str, str]:
    """Read the persisted ``{workspace_id: services_id}`` map from ``path``.

    Returns an empty dict for a missing file, a malformed JSON file, or any
    JSON whose top level is not an object of string-keyed string values; we
    never want a corrupt cache to break minds startup. The next successful
    discovery snapshot rewrites the file.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning("Could not read services-agent-id cache from {}: {}", path, exc)
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Services-agent-id cache at {} is not valid JSON: {}", path, exc)
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(k): str(v) for k, v in payload.items() if isinstance(k, str) and isinstance(v, str)}


def _write_services_agent_id_cache(path: Path, cache: dict[str, str]) -> None:
    """Persist the ``{workspace_id: services_id}`` map atomically to ``path``.

    Writes to a sibling ``.tmp`` file then renames to defend against a crash
    mid-write leaving a truncated file. A write failure logs a warning but
    does not propagate -- the cache is a best-effort fallback, and crashing
    the discovery thread over a transient I/O error would be worse than
    losing one update.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("Could not write services-agent-id cache to {}: {}", path, exc)


# -- Workspace cache helpers --


class WorkspaceCacheEntry(FrozenModel):
    """Persisted, display-only data for a workspace agent.

    Captured each time discovery surfaces a primary workspace agent, so the
    landing-page tile (and the chrome workspaces list) keep rendering when
    the live discovery snapshot transiently drops the agent -- the SSH-dead
    docker-container failure mode that motivates this cache existing in the
    first place. The cache is *not* consulted for control-flow decisions
    that need ground truth (e.g. the destroying-record DONE/FAILED status):
    ``list_known_workspace_ids`` continues to return live-only ids so those
    decisions remain authoritative.
    """

    name: str = Field(description="Human-readable workspace name (the ``workspace`` label value).")


def _workspace_cache_from_agents(agents: tuple[DiscoveredAgent, ...]) -> dict[str, WorkspaceCacheEntry]:
    """Snapshot primary workspace agents into ``{agent_id_str: WorkspaceCacheEntry}``.

    Only includes agents whose labels contain both ``workspace`` and
    ``is_primary`` -- the same filter ``list_known_workspace_ids`` applies --
    so the cache exactly mirrors what the live workspace list would emit.
    Returns an empty dict when no primary workspace agents are present in
    the current snapshot.
    """
    entries: dict[str, WorkspaceCacheEntry] = {}
    for agent in agents:
        labels = agent.labels
        if "workspace" not in labels or "is_primary" not in labels:
            continue
        entries[str(agent.agent_id)] = WorkspaceCacheEntry(name=labels.get("workspace", str(agent.agent_name)))
    return entries


def _read_workspace_cache(path: Path) -> dict[str, WorkspaceCacheEntry]:
    """Read the persisted ``{agent_id: WorkspaceCacheEntry}`` map from ``path``.

    Returns an empty dict for a missing file, a malformed JSON file, or any
    JSON whose top level is not an object of string-keyed entry-shaped values;
    we never want a corrupt cache to break minds startup. The next successful
    discovery snapshot rewrites the file.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning("Could not read workspace cache from {}: {}", path, exc)
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Workspace cache at {} is not valid JSON: {}", path, exc)
        return {}
    if not isinstance(payload, dict):
        return {}
    entries: dict[str, WorkspaceCacheEntry] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        name = value.get("name")
        if not isinstance(name, str):
            continue
        entries[key] = WorkspaceCacheEntry(name=name)
    return entries


def _write_workspace_cache(path: Path, cache: dict[str, WorkspaceCacheEntry]) -> None:
    """Persist the workspace cache atomically to ``path``.

    Writes to a sibling ``.tmp`` file then renames; a write failure logs a
    warning but does not propagate, matching the services-agent-id cache.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        serializable = {key: entry.model_dump(mode="json") for key, entry in cache.items()}
        tmp.write_text(json.dumps(serializable, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("Could not write workspace cache to {}: {}", path, exc)


# -- MngrCliBackendResolver --


class MngrCliBackendResolver(BackendResolverInterface):
    """Resolves backend URLs from continuously-updated state.

    State is updated externally via update_agents() and update_services() methods.
    In production, a MngrStreamManager calls these methods from background threads
    that stream data from `mngr observe --discovery-only` and `mngr event --follow`.

    All reads are thread-safe via an internal lock.
    """

    services_agent_cache_path: Path | None = Field(
        default=None,
        description=(
            "Optional JSON file recording each workspace agent's paired system-services "
            "agent id. Populated as discovery surfaces both agents on the same host; "
            "consulted by ``get_system_services_agent_id`` when live discovery has "
            "transiently dropped the pair (e.g. when the container's SSH transport "
            "goes down). When None, the cache is in-memory only."
        ),
    )

    workspaces_cache_path: Path | None = Field(
        default=None,
        description=(
            "Optional JSON file recording per-workspace display metadata "
            "(``WorkspaceCacheEntry``). Populated as discovery surfaces primary "
            "workspace agents; consulted by ``list_known_or_cached_workspace_ids`` "
            "and ``get_workspace_name`` so the landing-page tile and chrome workspaces "
            "list keep rendering when live discovery has transiently dropped the "
            "agent (the SSH-dead docker-container failure mode). Entries are evicted "
            "via ``evict_cached_workspace`` once minds confirms the workspace was "
            "destroyed. When None, the cache is in-memory only."
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
    _on_refresh_callbacks: list[Callable[[str, str], None]] = PrivateAttr(default_factory=list)
    # workspace_agent_id_str -> services_agent_id_str. Populated under _lock by
    # update_agents whenever discovery surfaces both agents on the same host.
    # Read by get_system_services_agent_id as a fallback for the case where
    # live discovery has lost the pair (the SSH-dead path that motivated this
    # cache existing in the first place).
    _services_agent_id_cache: dict[str, str] = PrivateAttr(default_factory=dict)
    # workspace_agent_id_str -> WorkspaceCacheEntry. Populated under _lock by
    # update_agents whenever discovery surfaces a primary workspace agent.
    # Read by list_known_or_cached_workspace_ids / get_workspace_name as a
    # fallback so the landing-page tile keeps rendering when live discovery
    # has lost the agent (the SSH-dead failure mode). Entries are evicted via
    # evict_cached_workspace once a destroy completes.
    _workspace_info_cache: dict[str, WorkspaceCacheEntry] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: object) -> None:
        """Load the persisted caches from disk, if configured."""
        if self.services_agent_cache_path is not None:
            self._services_agent_id_cache = _read_services_agent_id_cache(self.services_agent_cache_path)
        if self.workspaces_cache_path is not None:
            self._workspace_info_cache = _read_workspace_cache(self.workspaces_cache_path)

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

        Also refreshes two persistent caches from the new agent list:

        - The services-agent-id cache: each workspace agent that shares a
          host with a ``system-services`` agent gets an entry written through
          to ``services_agent_cache_path`` (if configured).
        - The workspace cache: each primary workspace agent in the snapshot
          gets a ``WorkspaceCacheEntry`` written through to
          ``workspaces_cache_path`` (if configured), so the landing page can
          keep rendering the tile when live discovery later transiently drops
          the agent (e.g. SSH dies inside the docker container).

        Existing cache entries are preserved when the current snapshot can't
        observe the pair / agent (i.e. transient discovery loss); that is
        the entire point of the caches. Workspace entries are evicted by
        ``evict_cached_workspace`` once a destroy completes.
        """
        services_path_to_write: Path | None = None
        services_cache_to_write: dict[str, str] | None = None
        workspaces_path_to_write: Path | None = None
        workspaces_cache_to_write: dict[str, WorkspaceCacheEntry] | None = None
        with self._lock:
            self._agents_result = result
            self._initial_discovery_done = True
            new_pairs = _services_pairs_from_agents(result.discovered_agents)
            if new_pairs:
                services_changed = False
                for workspace_id_str, services_id_str in new_pairs.items():
                    if self._services_agent_id_cache.get(workspace_id_str) != services_id_str:
                        self._services_agent_id_cache[workspace_id_str] = services_id_str
                        services_changed = True
                if services_changed and self.services_agent_cache_path is not None:
                    services_path_to_write = self.services_agent_cache_path
                    services_cache_to_write = dict(self._services_agent_id_cache)
            new_workspace_entries = _workspace_cache_from_agents(result.discovered_agents)
            if new_workspace_entries:
                workspaces_changed = False
                for workspace_id_str, entry in new_workspace_entries.items():
                    if self._workspace_info_cache.get(workspace_id_str) != entry:
                        self._workspace_info_cache[workspace_id_str] = entry
                        workspaces_changed = True
                if workspaces_changed and self.workspaces_cache_path is not None:
                    workspaces_path_to_write = self.workspaces_cache_path
                    workspaces_cache_to_write = dict(self._workspace_info_cache)
        if services_path_to_write is not None and services_cache_to_write is not None:
            _write_services_agent_id_cache(services_path_to_write, services_cache_to_write)
        if workspaces_path_to_write is not None and workspaces_cache_to_write is not None:
            _write_workspace_cache(workspaces_path_to_write, workspaces_cache_to_write)
        self._fire_on_change()

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
        """Return agent IDs that are primary workspace agents in the live snapshot.

        Filters for agents with both ``workspace`` and ``is_primary`` labels.

        This is deliberately live-only: ``destroying.read_destroying`` uses
        the answer to ``agent_id in list_known_workspace_ids()`` to derive
        DONE vs FAILED status, so including cached entries here would
        misclassify a successful destroy as FAILED. For UI-render callers
        (landing page, chrome workspaces list) that want the cached entries
        too, see :meth:`list_known_or_cached_workspace_ids`.
        """
        with self._lock:
            return tuple(
                agent.agent_id
                for agent in self._agents_result.discovered_agents
                if "workspace" in agent.labels and "is_primary" in agent.labels
            )

    def list_known_or_cached_workspace_ids(self) -> tuple[AgentId, ...]:
        """Return live primary workspace agent ids augmented with cached ids.

        Live ids first (preserving their order), followed by cached-only ids
        (i.e. cache entries whose agent is not present in the live snapshot)
        in sorted order for stable rendering. This is what the landing page
        and chrome workspaces list should call: a workspace whose docker
        container is up but whose sshd is dead drops out of the live
        snapshot, and we still want its tile to render so the user can
        trigger a restart.
        """
        with self._lock:
            live_ids = tuple(
                agent.agent_id
                for agent in self._agents_result.discovered_agents
                if "workspace" in agent.labels and "is_primary" in agent.labels
            )
            live_id_strs = {str(aid) for aid in live_ids}
            cached_only_id_strs = sorted(
                aid_str for aid_str in self._workspace_info_cache if aid_str not in live_id_strs
            )
        return live_ids + tuple(AgentId(aid_str) for aid_str in cached_only_id_strs)

    def evict_cached_workspace(self, agent_id: AgentId) -> None:
        """Drop ``agent_id``'s entry from the workspace cache (in-memory + on-disk).

        Called by the landing-page destroying-record handler once a destroy
        transitions to DONE (pid dead AND agent missing from the live
        resolver), which is the only signal we treat as authoritative proof
        that a workspace is truly gone. SSH-dead is explicitly not such a
        signal -- those cache entries persist across the outage.

        No-op when the entry is not in the cache.
        """
        path_to_write: Path | None = None
        cache_to_write: dict[str, WorkspaceCacheEntry] | None = None
        with self._lock:
            if str(agent_id) not in self._workspace_info_cache:
                return
            del self._workspace_info_cache[str(agent_id)]
            if self.workspaces_cache_path is not None:
                path_to_write = self.workspaces_cache_path
                cache_to_write = dict(self._workspace_info_cache)
        if path_to_write is not None and cache_to_write is not None:
            _write_workspace_cache(path_to_write, cache_to_write)
        self._fire_on_change()

    def get_workspace_name(self, agent_id: AgentId) -> str | None:
        """Return the workspace label value for an agent, or None.

        Falls back to the workspace cache when the live snapshot does not
        contain the agent, so a tile whose backing agent is transiently
        SSH-dead still renders with its proper human-readable name instead
        of degrading to the raw agent id.
        """
        agent_id_str = str(agent_id)
        with self._lock:
            for agent in self._agents_result.discovered_agents:
                if agent.agent_id == agent_id:
                    return agent.labels.get("workspace")
            cached = self._workspace_info_cache.get(agent_id_str)
            if cached is not None:
                return cached.name
            return None

    def get_ssh_info(self, agent_id: AgentId) -> RemoteSSHInfo | None:
        """Return SSH info for the agent's host, or None for local agents."""
        with self._lock:
            return self._agents_result.ssh_info_by_agent_id.get(str(agent_id))

    def get_system_services_agent_id(self, workspace_agent_id: AgentId) -> AgentId | None:
        """Return the ``system-services`` agent sharing the workspace agent's host.

        The workspace (claude) agent and the system-services agent run in the
        same container, so they share a host id. The lookup uses the current
        discovery snapshot first; if that snapshot does not contain the pair
        (the typical SSH-dead failure mode that motivates the recovery flow),
        falls back to the persisted services-agent-id cache so a restart can
        still address the system-services agent.
        """
        workspace_id_str = str(workspace_agent_id)
        with self._lock:
            host_id: str | None = None
            for agent in self._agents_result.discovered_agents:
                if agent.agent_id == workspace_agent_id:
                    host_id = str(agent.host_id)
                    break
            if host_id is not None:
                for agent in self._agents_result.discovered_agents:
                    if str(agent.host_id) == host_id and str(agent.agent_name) == SYSTEM_SERVICES_AGENT_NAME:
                        return agent.agent_id
            cached = self._services_agent_id_cache.get(workspace_id_str)
            if cached is not None:
                return AgentId(cached)
            return None

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        """Return display info from discovered agent data."""
        with self._lock:
            for agent in self._agents_result.discovered_agents:
                if agent.agent_id == agent_id:
                    return AgentDisplayInfo(
                        agent_name=str(agent.agent_name),
                        host_id=str(agent.host_id),
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

    def add_on_refresh_callback(self, callback: Callable[[str, str], None]) -> None:
        """Register a callback invoked when a refresh event arrives.

        The callback receives (agent_id_str, raw_json_line). Refresh events
        tell the desktop client to reload open web-service tabs for a service.
        """
        with self._lock:
            self._on_refresh_callbacks.append(callback)

    def remove_on_refresh_callback(self, callback: Callable[[str, str], None]) -> None:
        """Unregister a refresh event callback."""
        with self._lock:
            try:
                self._on_refresh_callbacks.remove(callback)
            except ValueError:
                pass

    def fire_on_refresh(self, agent_id_str: str, raw_line: str) -> None:
        """Invoke all registered refresh event callbacks.

        Public dispatch entry point used by both the legacy in-process
        ``MngrStreamManager`` and the new ``EnvelopeStreamConsumer``.
        """
        with self._lock:
            callbacks = list(self._on_refresh_callbacks)
        for callback in callbacks:
            try:
                callback(agent_id_str, raw_line)
            except (OSError, RuntimeError) as e:
                logger.warning("Refresh event callback failed: {}", e)

    def _fire_on_refresh(self, agent_id_str: str, raw_line: str) -> None:
        """Internal alias for ``fire_on_refresh`` retained for backward compatibility."""
        self.fire_on_refresh(agent_id_str, raw_line)
