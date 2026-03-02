import json
import time
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
from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mng.primitives import AgentId

SERVERS_LOG_FILENAME: Final[str] = "servers.jsonl"

_SUBPROCESS_TIMEOUT_SECONDS: Final[float] = 10.0

_CACHE_TTL_SECONDS: Final[float] = 5.0


class ServerLogRecord(FrozenModel):
    """A record of a server started by an agent, as written to servers.jsonl.

    Each line of servers.jsonl is a JSON object with these fields.
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


class MngCliInterface(MutableModel, ABC):
    """Interface for calling mng CLI commands.

    Production code uses SubprocessMngCli which shells out to the mng binary.
    Tests provide fake implementations that return canned responses.
    """

    @abstractmethod
    def read_agent_log(self, agent_id: AgentId, log_file: str) -> str | None:
        """Read an agent's log file via `mng logs`. Returns file contents or None on failure."""

    @abstractmethod
    def list_agents_json(self) -> str | None:
        """List agents via `mng list --json`. Returns JSON string or None on failure."""


class SubprocessMngCli(MngCliInterface):
    """Real implementation that shells out to the mng binary via ConcurrencyGroup."""

    def read_agent_log(self, agent_id: AgentId, log_file: str) -> str | None:
        cg = ConcurrencyGroup(name="mng-logs")
        try:
            with cg:
                result = cg.run_process_to_completion(
                    command=[MNG_BINARY, "logs", str(agent_id), log_file, "--quiet"],
                    timeout=_SUBPROCESS_TIMEOUT_SECONDS,
                    is_checked_after=False,
                )
        except ConcurrencyExceptionGroup as e:
            logger.warning("Failed to run mng logs for {}: {}", agent_id, e)
            return None

        if result.returncode != 0:
            logger.debug("mng logs returned non-zero for {}: {}", agent_id, result.stderr.strip())
            return None

        return result.stdout

    def list_agents_json(self) -> str | None:
        cg = ConcurrencyGroup(name="mng-list")
        try:
            with cg:
                result = cg.run_process_to_completion(
                    command=[MNG_BINARY, "list", "--json", "--quiet"],
                    timeout=_SUBPROCESS_TIMEOUT_SECONDS,
                    is_checked_after=False,
                )
        except ConcurrencyExceptionGroup as e:
            logger.warning("Failed to run mng list: {}", e)
            return None

        if result.returncode != 0:
            logger.warning("mng list failed: {}", result.stderr.strip())
            return None

        return result.stdout


# -- Parsing helpers (must be defined before MngCliBackendResolver) --


class _ParsedAgentsResult(FrozenModel):
    """Result of parsing mng list --json output."""

    agent_ids: tuple[AgentId, ...] = Field(default=(), description="All discovered agent IDs")
    ssh_info_by_agent_id: Mapping[str, RemoteSSHInfo] = Field(
        default_factory=dict,
        description="SSH info keyed by agent ID string, only for remote agents",
    )


def _parse_agents_from_json(json_output: str | None) -> _ParsedAgentsResult:
    """Parse agent IDs and SSH info from mng list --json output.

    Returns both agent IDs and a mapping of agent ID -> RemoteSSHInfo for agents
    that have SSH connection info (i.e., are running on remote hosts).
    """
    if json_output is None:
        return _ParsedAgentsResult()
    try:
        data = json.loads(json_output)
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse mng list output: {}", e)
        return _ParsedAgentsResult()

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

    return _ParsedAgentsResult(
        agent_ids=tuple(agent_ids),
        ssh_info_by_agent_id=ssh_info_by_id,
    )


def _parse_agent_ids_from_json(json_output: str | None) -> tuple[AgentId, ...]:
    """Parse agent IDs from mng list --json output, discarding SSH info."""
    return _parse_agents_from_json(json_output).agent_ids


def _parse_server_log_records(text: str) -> list[ServerLogRecord]:
    """Parse JSONL text into server log records, skipping invalid lines."""
    records: list[ServerLogRecord] = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            records.append(ServerLogRecord.model_validate(raw))
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Skipping invalid server log record: {}", e)
    return records


# -- MngCliBackendResolver --


class MngCliBackendResolver(BackendResolverInterface):
    """Resolves backend URLs by calling mng CLI commands.

    Uses `mng logs <agent-id> servers.jsonl` to read server info and
    `mng list --json` to discover agents. Results are cached with a short
    TTL to avoid excessive subprocess calls on every request.

    Each agent may have multiple servers listed in servers.jsonl (one per line).
    Later entries for the same server name override earlier ones.

    Also parses SSH info from `mng list --json` output to identify which agents
    are running on remote hosts. This info is used by the forwarding server to
    set up SSH tunnels for proxying traffic to remote backends.
    """

    mng_cli: MngCliInterface = Field(
        frozen=True,
        description="Interface for calling mng CLI commands",
    )

    _server_cache: dict[str, tuple[float, dict[str, str]]] = PrivateAttr(default_factory=dict)
    _agents_cache: tuple[float, _ParsedAgentsResult] | None = PrivateAttr(default=None)

    def _resolve_servers(self, agent_id: AgentId) -> dict[str, str]:
        """Get a mapping of server_name -> URL for an agent, using cache."""
        now = time.monotonic()
        cached = self._server_cache.get(str(agent_id))
        if cached is not None:
            cache_time, cached_servers = cached
            if (now - cache_time) < _CACHE_TTL_SECONDS:
                return cached_servers

        log_content = self.mng_cli.read_agent_log(agent_id, SERVERS_LOG_FILENAME)
        servers: dict[str, str] = {}
        if log_content is not None:
            records = _parse_server_log_records(log_content)
            for record in records:
                servers[str(record.server)] = record.url

        self._server_cache[str(agent_id)] = (now, servers)
        return servers

    def _get_agents_result(self) -> _ParsedAgentsResult:
        """Get cached parsed agents result, refreshing if stale."""
        now = time.monotonic()
        if self._agents_cache is not None:
            cache_time, cached = self._agents_cache
            if (now - cache_time) < _CACHE_TTL_SECONDS:
                return cached

        result = _parse_agents_from_json(self.mng_cli.list_agents_json())
        self._agents_cache = (now, result)
        return result

    def get_backend_url(self, agent_id: AgentId, server_name: ServerName) -> str | None:
        servers = self._resolve_servers(agent_id)
        return servers.get(str(server_name))

    def list_servers_for_agent(self, agent_id: AgentId) -> tuple[ServerName, ...]:
        servers = self._resolve_servers(agent_id)
        return tuple(ServerName(name) for name in sorted(servers.keys()))

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        return self._get_agents_result().agent_ids

    def get_ssh_info(self, agent_id: AgentId) -> RemoteSSHInfo | None:
        """Return SSH info for the agent's host, or None for local agents."""
        return self._get_agents_result().ssh_info_by_agent_id.get(str(agent_id))
