import json
import threading
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import paramiko
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import InvalidConcurrencyGroupStateError
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.desktop_client.ssh_tunnel import RemoteSSHInfo
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelError
from imbue.minds.primitives import ServiceName
from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.api.discovery_events import parse_discovery_event_line
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent

SERVICES_EVENT_SOURCE_NAME: Final[str] = "services"
REQUESTS_EVENT_SOURCE_NAME: Final[str] = "requests"
REFRESH_EVENT_SOURCE_NAME: Final[str] = "refresh"


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

    def get_work_dir(self, agent_id: AgentId) -> Path | None:
        """Return the agent's working directory on its host, or None if unknown.

        Used (for example) by the restart-workspace-server handler to locate the
        agent's services.toml when restarting a local mind. Default implementation
        returns None; subclasses with access to agent data should override.
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


# -- MngrCliBackendResolver --


class MngrCliBackendResolver(BackendResolverInterface):
    """Resolves backend URLs from continuously-updated state.

    State is updated externally via update_agents() and update_services() methods.
    In production, a MngrStreamManager calls these methods from background threads
    that stream data from `mngr observe --discovery-only` and `mngr events --follow`.

    All reads are thread-safe via an internal lock.
    """

    _agents_result: ParsedAgentsResult = PrivateAttr(default_factory=ParsedAgentsResult)
    _services_by_agent: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    _initial_discovery_done: bool = PrivateAttr(default=False)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _on_change_callbacks: list[Callable[[], None]] = PrivateAttr(default_factory=list)
    _on_request_callbacks: list[Callable[[str, str], None]] = PrivateAttr(default_factory=list)
    _on_refresh_callbacks: list[Callable[[str, str], None]] = PrivateAttr(default_factory=list)

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

    def update_agents(self, result: ParsedAgentsResult) -> None:
        """Replace the known agent list and SSH info. Thread-safe."""
        with self._lock:
            self._agents_result = result
            self._initial_discovery_done = True
        self._fire_on_change()

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

    def list_known_workspace_ids(self) -> tuple[AgentId, ...]:
        """Return agent IDs that are primary workspace agents.

        Filters for agents with both ``workspace`` and ``is_primary`` labels.
        """
        with self._lock:
            return tuple(
                agent.agent_id
                for agent in self._agents_result.discovered_agents
                if "workspace" in agent.labels and "is_primary" in agent.labels
            )

    def get_workspace_name(self, agent_id: AgentId) -> str | None:
        """Return the workspace label value for an agent, or None."""
        with self._lock:
            for agent in self._agents_result.discovered_agents:
                if agent.agent_id == agent_id:
                    return agent.labels.get("workspace")
            return None

    def get_ssh_info(self, agent_id: AgentId) -> RemoteSSHInfo | None:
        """Return SSH info for the agent's host, or None for local agents."""
        with self._lock:
            return self._agents_result.ssh_info_by_agent_id.get(str(agent_id))

    def get_work_dir(self, agent_id: AgentId) -> Path | None:
        """Return the agent's working directory (from its DiscoveredAgent data)."""
        with self._lock:
            for agent in self._agents_result.discovered_agents:
                if agent.agent_id == agent_id:
                    return agent.work_dir
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

    def _fire_on_request(self, agent_id_str: str, raw_line: str) -> None:
        """Invoke all registered request event callbacks."""
        with self._lock:
            callbacks = list(self._on_request_callbacks)
        for callback in callbacks:
            try:
                callback(agent_id_str, raw_line)
            except (OSError, RuntimeError) as e:
                logger.warning("Request event callback failed: {}", e)

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

    def _fire_on_refresh(self, agent_id_str: str, raw_line: str) -> None:
        """Invoke all registered refresh event callbacks."""
        with self._lock:
            callbacks = list(self._on_refresh_callbacks)
        for callback in callbacks:
            try:
                callback(agent_id_str, raw_line)
            except (OSError, RuntimeError) as e:
                logger.warning("Refresh event callback failed: {}", e)


# -- MngrStreamManager --


class MngrStreamManager(MutableModel):
    """Manages background streaming subprocesses that feed data to a MngrCliBackendResolver.

    Runs two types of long-lived subprocesses via ConcurrencyGroup:
    1. `mngr observe --discovery-only --quiet` to discover agents and hosts.
       Handles the following discovery event types:
       - DISCOVERY_FULL: replaces the entire agent list and agent-to-host mapping
       - HOST_SSH_INFO: updates SSH connection details for a specific host
       - AGENT_DISCOVERED: incrementally adds or updates a single agent
       - AGENT_DESTROYED: incrementally removes a single agent
       - HOST_DESTROYED: removes all agents on a destroyed host
    2. `mngr events <agent-id> servers --follow --quiet` (one per workspace agent)
       to discover each agent's servers.

    Only agents with the ``workspace`` label get events streams -- other agents
    are tracked in the resolver for completeness but their server events are
    not streamed.
    """

    resolver: MngrCliBackendResolver = Field(frozen=True, description="Backend resolver to update with streaming data")
    mngr_binary: str = Field(default=MNGR_BINARY, frozen=True, description="Path to mngr binary")

    _cg: ConcurrencyGroup = PrivateAttr(default_factory=lambda: ConcurrencyGroup(name="mngr-stream-manager"))
    _known_agent_ids: set[str] = PrivateAttr(default_factory=set)
    _agent_host_map: dict[str, str] = PrivateAttr(default_factory=dict)
    _discovered_agents: tuple[DiscoveredAgent, ...] = PrivateAttr(default=())
    _ssh_by_host_id: dict[str, RemoteSSHInfo] = PrivateAttr(default_factory=dict)
    _events_services: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    _observe_process: RunningProcess | None = PrivateAttr(default=None)
    _events_processes: dict[str, RunningProcess] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _on_agent_discovered_callbacks: list[Callable[[AgentId, RemoteSSHInfo | None, str], None]] = PrivateAttr(
        default_factory=list
    )

    def add_on_agent_discovered_callback(
        self,
        callback: Callable[[AgentId, RemoteSSHInfo | None, str], None],
    ) -> None:
        """Register a callback invoked when an agent is discovered.

        The callback receives the agent ID, SSH info (None for local agents),
        and the provider name (e.g. "docker", "local").
        """
        self._on_agent_discovered_callbacks.append(callback)

    def start(self) -> None:
        """Start the streaming subprocess for continuous agent discovery."""
        self._cg.__enter__()
        # Run from $HOME so mngr uses its global config, not any project-specific
        # .mngr/settings.toml that might restrict behavior (e.g. is_allowed_in_pytest).
        self._observe_process = self._cg.run_process_in_background(
            command=[self.mngr_binary, "observe", "--discovery-only", "--quiet"],
            on_output=self._on_discovery_stream_output,
            cwd=Path.home(),
        )

    def stop(self) -> None:
        """Stop all streaming subprocesses.

        Terminates the mngr observe and mngr events processes first so
        that the threads reading their output unblock immediately, then
        exits the ConcurrencyGroup (which joins the threads).
        """
        for process in self._all_managed_processes():
            process.terminate()
        self._cg.__exit__(None, None, None)

    def _all_managed_processes(self) -> list[RunningProcess]:
        """Return all managed subprocess handles (observe + per-agent events)."""
        result: list[RunningProcess] = []
        if self._observe_process is not None:
            result.append(self._observe_process)
        result.extend(self._events_processes.values())
        return result

    def _on_discovery_stream_output(self, line: str, is_stdout: bool) -> None:
        """Handle a line of output from mngr observe --discovery-only."""
        if not is_stdout:
            stripped = line.strip()
            if stripped:
                logger.debug("mngr observe stderr: {}", stripped)
            return
        stripped = line.strip()
        if not stripped:
            return
        self._handle_discovery_line(stripped)

    def _handle_discovery_line(self, line: str) -> None:
        """Parse a discovery event line and update state.

        Handles the following event types:
        - DISCOVERY_FULL: replaces the entire agent list and agent-to-host mapping
        - HOST_SSH_INFO: updates SSH info for a specific host
        - AGENT_DISCOVERED: incrementally adds or updates a single agent
        - AGENT_DESTROYED: incrementally removes a single agent
        - HOST_DESTROYED: removes all agents that were on the destroyed host
        """
        try:
            event = parse_discovery_event_line(line)
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("Failed to parse discovery event line: {} (line: {})", e, line[:200])
            return

        if isinstance(event, FullDiscoverySnapshotEvent):
            self._handle_full_snapshot(event)
        elif isinstance(event, HostSSHInfoEvent):
            self._handle_host_ssh_info(event)
        elif isinstance(event, AgentDiscoveryEvent):
            self._handle_agent_discovered(event)
        elif isinstance(event, AgentDestroyedEvent):
            self._handle_agent_destroyed(event)
        elif isinstance(event, HostDestroyedEvent):
            self._handle_host_destroyed(event)
        elif event is None:
            logger.warning("Unrecognized discovery event line: {}", line[:200])
        else:
            logger.trace("Ignoring discovery event: {}", type(event).__name__)

    @staticmethod
    def _is_workspace_agent(agent: DiscoveredAgent) -> bool:
        """Check whether a discovered agent has the ``workspace`` label."""
        return "workspace" in agent.labels

    def _handle_full_snapshot(self, event: FullDiscoverySnapshotEvent) -> None:
        """Update agent list and agent-to-host mapping from a full snapshot."""
        agent_ids: list[AgentId] = []
        agent_host_map: dict[str, str] = {}
        for agent in event.agents:
            agent_ids.append(agent.agent_id)
            agent_host_map[str(agent.agent_id)] = str(agent.host_id)

        workspace_names = [str(a.agent_name) for a in event.agents if self._is_workspace_agent(a)]
        logger.debug(
            "Processing DISCOVERY_FULL: {} agents total, {} workspace agents: {}",
            len(event.agents),
            len(workspace_names),
            workspace_names,
        )

        with self._lock:
            self._agent_host_map = agent_host_map

        self._update_resolver(tuple(agent_ids), event.agents)

        workspace_ids = {str(agent.agent_id) for agent in event.agents if self._is_workspace_agent(agent)}
        self._sync_events_streams(workspace_ids)

        # Notify callbacks for all discovered agents
        for agent in event.agents:
            ssh_info = self._get_ssh_info_for_agent(agent.agent_id)
            self._fire_agent_discovered_callbacks(agent.agent_id, ssh_info, str(agent.provider_name))

    def _handle_host_ssh_info(self, event: HostSSHInfoEvent) -> None:
        """Update SSH info for a host and refresh the resolver."""
        ssh_info = RemoteSSHInfo(
            user=event.ssh.user,
            host=event.ssh.host,
            port=event.ssh.port,
            key_path=event.ssh.key_path,
        )
        host_id_str = str(event.host_id)
        with self._lock:
            self._ssh_by_host_id[host_id_str] = ssh_info
            agent_ids = tuple(AgentId(agent_id) for agent_id in self._agent_host_map)
            # Find agents on this host so we can notify discovery callbacks with SSH info
            agents_on_host = tuple(AgentId(aid) for aid, hid in self._agent_host_map.items() if hid == host_id_str)

        self._update_resolver(agent_ids)

        # Re-fire callbacks for agents on this host now that SSH info is available.
        # This handles the case where agent discovery fires before SSH info arrives.
        for agent_id in agents_on_host:
            provider = self._get_provider_name_for_agent(agent_id)
            self._fire_agent_discovered_callbacks(agent_id, ssh_info, provider)

    def _handle_agent_discovered(self, event: AgentDiscoveryEvent) -> None:
        """Incrementally add or update a single agent in the resolver."""
        agent = event.agent
        aid_str = str(agent.agent_id)
        is_workspace = self._is_workspace_agent(agent)
        logger.debug(
            "AGENT_DISCOVERED: {} (workspace={}, labels={})",
            agent.agent_name,
            is_workspace,
            list(agent.labels.keys()),
        )

        with self._lock:
            self._agent_host_map[aid_str] = str(agent.host_id)
            # Replace existing entry or append
            updated_agents = [a for a in self._discovered_agents if str(a.agent_id) != aid_str]
            updated_agents.append(agent)
            self._discovered_agents = tuple(updated_agents)
            # Start events stream if this is a newly discovered workspace agent
            is_new = aid_str not in self._known_agent_ids
            if is_new and self._is_workspace_agent(agent):
                self._known_agent_ids.add(aid_str)
                self._start_events_stream(agent.agent_id)
            agent_ids = tuple(AgentId(aid) for aid in self._agent_host_map)
            discovered_agents = self._discovered_agents

        self._update_resolver(agent_ids, discovered_agents)

        # Notify callbacks
        ssh_info = self._get_ssh_info_for_agent(agent.agent_id)
        self._fire_agent_discovered_callbacks(agent.agent_id, ssh_info, str(agent.provider_name))

    def _handle_agent_destroyed(self, event: AgentDestroyedEvent) -> None:
        """Remove a destroyed agent from the resolver and stop its events stream."""
        aid_str = str(event.agent_id)

        with self._lock:
            self._agent_host_map.pop(aid_str, None)
            self._discovered_agents = tuple(a for a in self._discovered_agents if str(a.agent_id) != aid_str)
            self._known_agent_ids.discard(aid_str)
            process = self._events_processes.pop(aid_str, None)
            if process is not None:
                process.terminate()
            self._events_services.pop(aid_str, None)
            agent_ids = tuple(AgentId(aid) for aid in self._agent_host_map)
            discovered_agents = self._discovered_agents

        self._update_resolver(agent_ids, discovered_agents)
        self.resolver.update_services(event.agent_id, {})

    def _handle_host_destroyed(self, event: HostDestroyedEvent) -> None:
        """Remove all agents on a destroyed host from the resolver."""
        destroyed_ids = {str(agent_id) for agent_id in event.agent_ids}

        with self._lock:
            for aid_str in destroyed_ids:
                self._agent_host_map.pop(aid_str, None)
                self._known_agent_ids.discard(aid_str)
                process = self._events_processes.pop(aid_str, None)
                if process is not None:
                    process.terminate()
                self._events_services.pop(aid_str, None)
            self._discovered_agents = tuple(a for a in self._discovered_agents if str(a.agent_id) not in destroyed_ids)
            self._ssh_by_host_id.pop(str(event.host_id), None)
            agent_ids = tuple(AgentId(aid) for aid in self._agent_host_map)
            discovered_agents = self._discovered_agents

        self._update_resolver(agent_ids, discovered_agents)
        for agent_id in event.agent_ids:
            self.resolver.update_services(agent_id, {})

    def _get_provider_name_for_agent(self, agent_id: AgentId) -> str:
        """Look up the provider name for an agent. Returns 'unknown' if not found."""
        with self._lock:
            for agent in self._discovered_agents:
                if agent.agent_id == agent_id:
                    return str(agent.provider_name)
        return "unknown"

    def _get_ssh_info_for_agent(self, agent_id: AgentId) -> RemoteSSHInfo | None:
        """Look up SSH info for an agent from the host mapping."""
        with self._lock:
            host_id = self._agent_host_map.get(str(agent_id))
            if host_id is None:
                return None
            return self._ssh_by_host_id.get(host_id)

    def _fire_agent_discovered_callbacks(
        self,
        agent_id: AgentId,
        ssh_info: RemoteSSHInfo | None,
        provider_name: str,
    ) -> None:
        """Invoke all registered on_agent_discovered callbacks."""
        for callback in self._on_agent_discovered_callbacks:
            try:
                callback(agent_id, ssh_info, provider_name)
            except (OSError, ValueError, RuntimeError, paramiko.SSHException, SSHTunnelError) as e:
                logger.warning("Agent discovery callback failed for {}: {}", agent_id, e)

    def _update_resolver(
        self,
        agent_ids: tuple[AgentId, ...],
        discovered_agents: tuple[DiscoveredAgent, ...] | None = None,
    ) -> None:
        """Rebuild and push the ParsedAgentsResult to the resolver."""
        with self._lock:
            ssh_info_by_agent_id: dict[str, RemoteSSHInfo] = {}
            for aid_str, host_id_str in self._agent_host_map.items():
                ssh = self._ssh_by_host_id.get(host_id_str)
                if ssh is not None:
                    ssh_info_by_agent_id[aid_str] = ssh
            if discovered_agents is not None:
                self._discovered_agents = discovered_agents
            agents = self._discovered_agents

        self.resolver.update_agents(
            ParsedAgentsResult(
                agent_ids=agent_ids,
                discovered_agents=agents,
                ssh_info_by_agent_id=ssh_info_by_agent_id,
            )
        )

    def _sync_events_streams(self, new_agent_ids: set[str]) -> None:
        """Start events streams for new agents and stop streams for removed agents."""
        with self._lock:
            previously_known = set(self._known_agent_ids)
            self._known_agent_ids = new_agent_ids

            removed = previously_known - new_agent_ids
            added = new_agent_ids - previously_known
            if removed or added:
                logger.info("Syncing events streams: added={}, removed={}", added, removed)

            # Stop streams for agents that are no longer present
            for aid_str in removed:
                process = self._events_processes.pop(aid_str, None)
                if process is not None:
                    logger.info("Stopping events stream for removed agent {}", aid_str)
                    process.terminate()
                self._events_services.pop(aid_str, None)

            # Start streams for newly discovered agents
            for aid_str in added:
                self._start_events_stream(AgentId(aid_str))

    def _on_events_stream_output(self, line: str, is_stdout: bool, agent_id: AgentId) -> None:
        """Handle a line of output from mngr events --follow for a specific agent.

        Dispatches based on the ``source`` field in the event envelope:
        service events update the resolver's service map, request events are
        forwarded to registered request callbacks, and refresh events are
        forwarded to registered refresh callbacks.
        """
        if not is_stdout:
            stripped = line.strip()
            if stripped:
                logger.debug("mngr events stderr for {}: {}", agent_id, stripped)
            return
        stripped = line.strip()
        if not stripped:
            return
        aid_str = str(agent_id)
        try:
            raw = json.loads(stripped)
            source = raw.get("source", "")
            if source == REQUESTS_EVENT_SOURCE_NAME:
                self.resolver._fire_on_request(aid_str, stripped)
                return
            if source == REFRESH_EVENT_SOURCE_NAME:
                self.resolver._fire_on_refresh(aid_str, stripped)
                return
            record = parse_service_log_record(raw)
            services = self._events_services.get(aid_str)
            if services is None:
                return
            if isinstance(record, ServiceDeregisteredRecord):
                services.pop(str(record.service), None)
            else:
                services[str(record.service)] = record.url
            self.resolver.update_services(agent_id, dict(services))
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("Failed to parse event line for {}: {} (line: {})", agent_id, e, stripped[:200])

    def _start_events_stream(self, agent_id: AgentId) -> None:
        """Start mngr events <agent-id> services requests refresh --follow for a workspace agent."""
        if self._cg.is_shutting_down():
            logger.debug("Skipping events stream for {} -- shutting down", agent_id)
            return

        aid_str = str(agent_id)
        self._events_services[aid_str] = {}

        logger.info("Starting events stream for agent {}", aid_str)
        try:
            process = self._cg.run_process_in_background(
                command=[
                    self.mngr_binary,
                    "events",
                    aid_str,
                    SERVICES_EVENT_SOURCE_NAME,
                    REQUESTS_EVENT_SOURCE_NAME,
                    REFRESH_EVENT_SOURCE_NAME,
                    "--follow",
                    "--quiet",
                ],
                on_output=lambda line, is_stdout: self._on_events_stream_output(line, is_stdout, agent_id),
                cwd=Path.home(),
            )
            self._events_processes[aid_str] = process
        except InvalidConcurrencyGroupStateError:
            logger.debug("Cannot start events stream for {} -- concurrency group is no longer active", agent_id)
