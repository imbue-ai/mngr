import json
import threading
from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.forwarding_server.ssh_tunnel import RemoteSSHInfo
from imbue.minds.primitives import ServerName
from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.api.discovery_events import parse_discovery_event_line
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent

SERVERS_EVENT_SOURCE_NAME: Final[str] = "servers"


class ServerLogParseError(ValueError):
    """Raised when a server log record cannot be parsed."""


class ServerLogRecord(FrozenModel):
    """A record of a server started by an agent, as written to servers/events.jsonl.

    Each line of servers/events.jsonl is a JSON object with these fields.
    Agents write these records on startup so the forwarding server can discover them.
    """

    server: ServerName = Field(description="Name of the server (e.g., 'web')")
    url: str = Field(description="URL where the server is accessible (e.g., 'http://127.0.0.1:9100')")


class BackendResolverInterface(MutableModel, ABC):
    """Resolves agent IDs and server names to their backend server URLs.

    Each agent may run multiple servers (e.g. 'web', 'api'), each accessible
    at a different URL. The resolver maps (agent_id, server_name) pairs to URLs.
    """

    @abstractmethod
    def get_backend_url(self, agent_id: AgentId, server_name: ServerName) -> str | None:
        """Return the backend URL for a specific server of an agent, or None if unknown/offline."""

    @abstractmethod
    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        """Return all known agent IDs."""

    def list_known_mind_ids(self) -> tuple[AgentId, ...]:
        """Return agent IDs that have the mind=true label.

        Default implementation returns all known agent IDs (no filtering).
        Subclasses with access to agent labels should override this.
        """
        return self.list_known_agent_ids()

    @abstractmethod
    def list_servers_for_agent(self, agent_id: AgentId) -> tuple[ServerName, ...]:
        """Return all known server names for an agent, sorted alphabetically."""

    def get_ssh_info(self, agent_id: AgentId) -> RemoteSSHInfo | None:
        """Return SSH connection info for the agent's host, or None for local agents.

        Default implementation returns None (all agents treated as local).
        Subclasses that discover remote agents should override this.
        """
        return None


class StaticBackendResolver(BackendResolverInterface):
    """Resolves backend URLs from a static mapping provided at construction time.

    The mapping is structured as {agent_id: {server_name: url}}.
    """

    url_by_agent_and_server: Mapping[str, Mapping[str, str]] = Field(
        frozen=True,
        description="Mapping of agent ID to mapping of server name to backend URL",
    )

    def get_backend_url(self, agent_id: AgentId, server_name: ServerName) -> str | None:
        servers = self.url_by_agent_and_server.get(str(agent_id))
        if servers is None:
            return None
        return servers.get(str(server_name))

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        return tuple(AgentId(agent_id) for agent_id in sorted(self.url_by_agent_and_server.keys()))

    def list_servers_for_agent(self, agent_id: AgentId) -> tuple[ServerName, ...]:
        servers = self.url_by_agent_and_server.get(str(agent_id))
        if servers is None:
            return ()
        return tuple(ServerName(name) for name in sorted(servers.keys()))


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


def parse_server_log_record(raw: dict[str, object]) -> ServerLogRecord:
    """Parse a single JSON dict into a ServerLogRecord.

    Extracts only the 'server' and 'url' fields, ignoring any extra
    envelope fields (timestamp, event_id, source, type) that may be present.
    Raises ValueError if required fields are missing.
    """
    server = raw.get("server")
    url = raw.get("url")
    if not server or not url:
        raise ServerLogParseError(f"Server log record missing required fields (server={server!r}, url={url!r})")
    return ServerLogRecord(server=ServerName(str(server)), url=str(url))


def parse_server_log_records(text: str) -> list[ServerLogRecord]:
    """Parse JSONL text into server log records.

    Extracts only the 'server' and 'url' fields, ignoring any extra
    envelope fields (timestamp, event_id, source, type) that may be present.
    Raises on malformed lines rather than silently skipping them.
    """
    records: list[ServerLogRecord] = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        raw = json.loads(line)
        records.append(parse_server_log_record(raw))
    return records


# -- MngrCliBackendResolver --


class MngrCliBackendResolver(BackendResolverInterface):
    """Resolves backend URLs from continuously-updated state.

    State is updated externally via update_agents() and update_servers() methods.
    In production, a MngrStreamManager calls these methods from background threads
    that stream data from `mngr observe --discovery-only` and `mngr events --follow`.

    All reads are thread-safe via an internal lock.
    """

    _agents_result: ParsedAgentsResult = PrivateAttr(default_factory=ParsedAgentsResult)
    _servers_by_agent: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def update_agents(self, result: ParsedAgentsResult) -> None:
        """Replace the known agent list and SSH info. Thread-safe."""
        with self._lock:
            self._agents_result = result

    def update_servers(self, agent_id: AgentId, servers: dict[str, str]) -> None:
        """Replace the known servers for a single agent. Thread-safe."""
        with self._lock:
            self._servers_by_agent[str(agent_id)] = servers

    def get_backend_url(self, agent_id: AgentId, server_name: ServerName) -> str | None:
        with self._lock:
            servers = self._servers_by_agent.get(str(agent_id), {})
            return servers.get(str(server_name))

    def list_servers_for_agent(self, agent_id: AgentId) -> tuple[ServerName, ...]:
        with self._lock:
            servers = self._servers_by_agent.get(str(agent_id), {})
            return tuple(ServerName(name) for name in sorted(servers.keys()))

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        with self._lock:
            return self._agents_result.agent_ids

    def list_known_mind_ids(self) -> tuple[AgentId, ...]:
        """Return agent IDs that have the mind label set."""
        with self._lock:
            return tuple(agent.agent_id for agent in self._agents_result.discovered_agents if "mind" in agent.labels)

    def get_ssh_info(self, agent_id: AgentId) -> RemoteSSHInfo | None:
        """Return SSH info for the agent's host, or None for local agents."""
        with self._lock:
            return self._agents_result.ssh_info_by_agent_id.get(str(agent_id))


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
    2. `mngr events <agent-id> servers/events.jsonl --follow --quiet` (one per agent)
       to discover each agent's servers.
    """

    resolver: MngrCliBackendResolver = Field(frozen=True, description="Backend resolver to update with streaming data")
    mngr_binary: str = Field(default=MNGR_BINARY, frozen=True, description="Path to mngr binary")

    _cg: ConcurrencyGroup = PrivateAttr(default_factory=lambda: ConcurrencyGroup(name="mngr-stream-manager"))
    _known_agent_ids: set[str] = PrivateAttr(default_factory=set)
    _agent_host_map: dict[str, str] = PrivateAttr(default_factory=dict)
    _discovered_agents: tuple[DiscoveredAgent, ...] = PrivateAttr(default=())
    _ssh_by_host_id: dict[str, RemoteSSHInfo] = PrivateAttr(default_factory=dict)
    _events_servers: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    _events_processes: dict[str, RunningProcess] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def start(self) -> None:
        """Start the streaming subprocess for continuous agent discovery."""
        self._cg.__enter__()
        self._cg.run_process_in_background(
            command=[self.mngr_binary, "observe", "--discovery-only", "--quiet"],
            on_output=self._on_discovery_stream_output,
        )

    def stop(self) -> None:
        """Stop all streaming subprocesses."""
        self._cg.__exit__(None, None, None)

    def _on_discovery_stream_output(self, line: str, is_stdout: bool) -> None:
        """Handle a line of output from mngr observe --discovery-only."""
        if not is_stdout:
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

    def _handle_full_snapshot(self, event: FullDiscoverySnapshotEvent) -> None:
        """Update agent list and agent-to-host mapping from a full snapshot."""
        agent_ids: list[AgentId] = []
        agent_host_map: dict[str, str] = {}
        for agent in event.agents:
            agent_ids.append(agent.agent_id)
            agent_host_map[str(agent.agent_id)] = str(agent.host_id)

        with self._lock:
            self._agent_host_map = agent_host_map

        self._update_resolver(tuple(agent_ids), event.agents)

        new_ids = {str(agent_id) for agent_id in agent_ids}
        self._sync_events_streams(new_ids)

    def _handle_host_ssh_info(self, event: HostSSHInfoEvent) -> None:
        """Update SSH info for a host and refresh the resolver."""
        ssh_info = RemoteSSHInfo(
            user=event.ssh.user,
            host=event.ssh.host,
            port=event.ssh.port,
            key_path=event.ssh.key_path,
        )
        with self._lock:
            self._ssh_by_host_id[str(event.host_id)] = ssh_info
            agent_ids = tuple(AgentId(agent_id) for agent_id in self._agent_host_map)

        self._update_resolver(agent_ids)

    def _handle_agent_discovered(self, event: AgentDiscoveryEvent) -> None:
        """Incrementally add or update a single agent in the resolver."""
        agent = event.agent
        aid_str = str(agent.agent_id)

        with self._lock:
            self._agent_host_map[aid_str] = str(agent.host_id)
            # Replace existing entry or append
            updated_agents = [a for a in self._discovered_agents if str(a.agent_id) != aid_str]
            updated_agents.append(agent)
            self._discovered_agents = tuple(updated_agents)
            # Start events stream if this is a newly discovered agent
            is_new = aid_str not in self._known_agent_ids
            if is_new:
                self._known_agent_ids.add(aid_str)
                self._start_events_stream(agent.agent_id)
            agent_ids = tuple(AgentId(aid) for aid in self._agent_host_map)
            discovered_agents = self._discovered_agents

        self._update_resolver(agent_ids, discovered_agents)

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
            self._events_servers.pop(aid_str, None)
            agent_ids = tuple(AgentId(aid) for aid in self._agent_host_map)
            discovered_agents = self._discovered_agents

        self._update_resolver(agent_ids, discovered_agents)
        self.resolver.update_servers(event.agent_id, {})

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
                self._events_servers.pop(aid_str, None)
            self._discovered_agents = tuple(a for a in self._discovered_agents if str(a.agent_id) not in destroyed_ids)
            self._ssh_by_host_id.pop(str(event.host_id), None)
            agent_ids = tuple(AgentId(aid) for aid in self._agent_host_map)
            discovered_agents = self._discovered_agents

        self._update_resolver(agent_ids, discovered_agents)
        for agent_id in event.agent_ids:
            self.resolver.update_servers(agent_id, {})

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

            # Stop streams for agents that are no longer present
            for aid_str in previously_known - new_agent_ids:
                process = self._events_processes.pop(aid_str, None)
                if process is not None:
                    process.terminate()
                self._events_servers.pop(aid_str, None)

            # Start streams for newly discovered agents
            for aid_str in new_agent_ids - previously_known:
                self._start_events_stream(AgentId(aid_str))

    def _on_events_stream_output(self, line: str, is_stdout: bool, agent_id: AgentId) -> None:
        """Handle a line of output from mngr events --follow for a specific agent."""
        if not is_stdout:
            return
        stripped = line.strip()
        if not stripped:
            return
        aid_str = str(agent_id)
        try:
            raw = json.loads(stripped)
            record = parse_server_log_record(raw)
            servers = self._events_servers.get(aid_str)
            if servers is None:
                return
            servers[str(record.server)] = record.url
            self.resolver.update_servers(agent_id, dict(servers))
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("Failed to parse server log line for {}: {} (line: {})", agent_id, e, stripped[:200])

    def _start_events_stream(self, agent_id: AgentId) -> None:
        """Start mngr events <agent-id> servers/events.jsonl --follow for a single agent."""
        aid_str = str(agent_id)
        self._events_servers[aid_str] = {}

        process = self._cg.run_process_in_background(
            command=[self.mngr_binary, "events", aid_str, SERVERS_EVENT_SOURCE_NAME, "--follow", "--quiet"],
            on_output=lambda line, is_stdout: self._on_events_stream_output(line, is_stdout, agent_id),
        )
        self._events_processes[aid_str] = process
