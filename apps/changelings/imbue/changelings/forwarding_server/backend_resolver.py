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

from imbue.changelings.config.data_types import MNG_BINARY
from imbue.changelings.forwarding_server.ssh_tunnel import RemoteSSHInfo
from imbue.changelings.primitives import ServerName
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mng.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mng.api.discovery_events import HostSSHInfoEvent
from imbue.mng.api.discovery_events import parse_discovery_event_line
from imbue.mng.primitives import AgentId

SERVERS_LOG_FILENAME: Final[str] = "servers/events.jsonl"


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
    """Result of parsing agent and SSH info from discovery events or mng list --json output."""

    agent_ids: tuple[AgentId, ...] = Field(default=(), description="All discovered agent IDs")
    ssh_info_by_agent_id: Mapping[str, RemoteSSHInfo] = Field(
        default_factory=dict,
        description="SSH info keyed by agent ID string, only for remote agents",
    )


def parse_agents_from_json(json_output: str | None) -> ParsedAgentsResult:
    """Parse agent IDs and SSH info from mng list --json output.

    Returns both agent IDs and a mapping of agent ID -> RemoteSSHInfo for agents
    that have SSH connection info (i.e., are running on remote hosts).
    """
    if json_output is None:
        return ParsedAgentsResult()
    try:
        data = json.loads(json_output)
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse mng list output: {}", e)
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
    """Parse agent IDs from mng list --json output, discarding SSH info."""
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


# -- MngCliBackendResolver --


class MngCliBackendResolver(BackendResolverInterface):
    """Resolves backend URLs from continuously-updated state.

    State is updated externally via update_agents() and update_servers() methods.
    In production, a MngStreamManager calls these methods from background threads
    that stream data from `mng list --stream` and `mng events --follow`.

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

    def get_ssh_info(self, agent_id: AgentId) -> RemoteSSHInfo | None:
        """Return SSH info for the agent's host, or None for local agents."""
        with self._lock:
            return self._agents_result.ssh_info_by_agent_id.get(str(agent_id))


# -- MngStreamManager --


class MngStreamManager(MutableModel):
    """Manages background streaming subprocesses that feed data to a MngCliBackendResolver.

    Runs two types of long-lived subprocesses via ConcurrencyGroup:
    1. `mng list --stream --quiet` to discover agents and hosts.
       Parses DISCOVERY_FULL events for the agent list and agent-to-host mapping,
       and HOST_SSH_INFO events for SSH connection details per host.
    2. `mng events <agent-id> servers/events.jsonl --follow --quiet` (one per agent)
       to discover each agent's servers.
    """

    resolver: MngCliBackendResolver = Field(frozen=True, description="Backend resolver to update with streaming data")
    mng_binary: str = Field(default=MNG_BINARY, frozen=True, description="Path to mng binary")

    _cg: ConcurrencyGroup = PrivateAttr(default_factory=lambda: ConcurrencyGroup(name="mng-stream-manager"))
    _known_agent_ids: set[str] = PrivateAttr(default_factory=set)
    _agent_host_map: dict[str, str] = PrivateAttr(default_factory=dict)
    _ssh_by_host_id: dict[str, RemoteSSHInfo] = PrivateAttr(default_factory=dict)
    _events_servers: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    _events_processes: dict[str, RunningProcess] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def start(self) -> None:
        """Start the streaming subprocess for continuous agent discovery."""
        self._cg.__enter__()
        self._cg.run_process_in_background(
            command=[self.mng_binary, "list", "--stream", "--quiet"],
            on_output=self._on_list_stream_output,
        )

    def stop(self) -> None:
        """Stop all streaming subprocesses."""
        self._cg.__exit__(None, None, None)

    def _on_list_stream_output(self, line: str, is_stdout: bool) -> None:
        """Handle a line of output from mng list --stream."""
        if not is_stdout:
            return
        stripped = line.strip()
        if not stripped:
            return
        self._handle_discovery_line(stripped)

    def _handle_discovery_line(self, line: str) -> None:
        """Parse a discovery event line and update state.

        Handles two event types:
        - DISCOVERY_FULL: updates agent list and agent-to-host mapping
        - HOST_SSH_INFO: updates SSH info for a specific host

        Both event types trigger a resolver update with the current SSH mappings.
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
        elif event is None:
            logger.warning("Unrecognized discovery event line: {}", line[:200])
        else:
            logger.trace("Ignoring non-snapshot discovery event: {}", type(event).__name__)

    def _handle_full_snapshot(self, event: FullDiscoverySnapshotEvent) -> None:
        """Update agent list and agent-to-host mapping from a full snapshot."""
        agent_ids: list[AgentId] = []
        agent_host_map: dict[str, str] = {}
        for agent in event.agents:
            agent_ids.append(agent.agent_id)
            agent_host_map[str(agent.agent_id)] = str(agent.host_id)

        with self._lock:
            self._agent_host_map = agent_host_map

        self._update_resolver(tuple(agent_ids))

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

    def _update_resolver(self, agent_ids: tuple[AgentId, ...]) -> None:
        """Rebuild and push the ParsedAgentsResult to the resolver."""
        with self._lock:
            ssh_info_by_agent_id: dict[str, RemoteSSHInfo] = {}
            for aid_str, host_id_str in self._agent_host_map.items():
                ssh = self._ssh_by_host_id.get(host_id_str)
                if ssh is not None:
                    ssh_info_by_agent_id[aid_str] = ssh

        self.resolver.update_agents(
            ParsedAgentsResult(
                agent_ids=agent_ids,
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
        """Handle a line of output from mng events --follow for a specific agent."""
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
        """Start mng events <agent-id> servers/events.jsonl --follow for a single agent."""
        aid_str = str(agent_id)
        self._events_servers[aid_str] = {}

        process = self._cg.run_process_in_background(
            command=[self.mng_binary, "events", aid_str, SERVERS_LOG_FILENAME, "--follow", "--quiet"],
            on_output=lambda line, is_stdout: self._on_events_stream_output(line, is_stdout, agent_id),
        )
        self._events_processes[aid_str] = process
