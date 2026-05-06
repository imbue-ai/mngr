import json
import threading
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.ssh_tunnel import RemoteSSHInfo
from imbue.minds.primitives import ServiceName
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
    that stream data from `mngr observe --discovery-only` and `mngr event --follow`.

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

    def notify_change(self) -> None:
        """Public wake-up for SSE listeners after external state mutations.

        ``_fire_on_change`` is fired internally on agent/service updates, but
        the request inbox lives outside this resolver. Inbox mutations
        (new request events, mirrored response events) call this so chrome
        SSE consumers don't have to wait for the next 30s poll tick.
        """
        self._fire_on_change()

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
