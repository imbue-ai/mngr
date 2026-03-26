import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from imbue.minds.forwarding_server.backend_resolver import BackendResolverInterface
from imbue.minds.forwarding_server.backend_resolver import MngCliBackendResolver
from imbue.minds.forwarding_server.backend_resolver import MngStreamManager
from imbue.minds.forwarding_server.backend_resolver import ParsedAgentsResult
from imbue.minds.forwarding_server.backend_resolver import ServerLogParseError
from imbue.minds.forwarding_server.backend_resolver import StaticBackendResolver
from imbue.minds.forwarding_server.backend_resolver import parse_agent_ids_from_json
from imbue.minds.forwarding_server.backend_resolver import parse_agents_from_json
from imbue.minds.forwarding_server.backend_resolver import parse_server_log_records
from imbue.minds.forwarding_server.conftest import make_agents_json
from imbue.minds.forwarding_server.conftest import make_resolver_with_data
from imbue.minds.forwarding_server.conftest import make_server_log
from imbue.minds.primitives import ServerName
from imbue.mng.primitives import AgentId

_AGENT_A: AgentId = AgentId("agent-00000000000000000000000000000001")
_AGENT_B: AgentId = AgentId("agent-00000000000000000000000000000002")
_SERVER_WEB: ServerName = ServerName("web")
_SERVER_API: ServerName = ServerName("api")


# -- StaticBackendResolver tests --


def test_static_get_backend_url_returns_url_for_known_agent_and_server() -> None:
    resolver = StaticBackendResolver(
        url_by_agent_and_server={str(_AGENT_A): {"web": "http://localhost:3001"}},
    )
    url = resolver.get_backend_url(_AGENT_A, _SERVER_WEB)
    assert url == "http://localhost:3001"


def test_static_get_backend_url_returns_none_for_unknown_agent() -> None:
    resolver = StaticBackendResolver(
        url_by_agent_and_server={str(_AGENT_A): {"web": "http://localhost:3001"}},
    )
    url = resolver.get_backend_url(_AGENT_B, _SERVER_WEB)
    assert url is None


def test_static_get_backend_url_returns_none_for_unknown_server() -> None:
    resolver = StaticBackendResolver(
        url_by_agent_and_server={str(_AGENT_A): {"web": "http://localhost:3001"}},
    )
    url = resolver.get_backend_url(_AGENT_A, _SERVER_API)
    assert url is None


def test_static_list_known_agent_ids_returns_sorted_ids() -> None:
    resolver = StaticBackendResolver(
        url_by_agent_and_server={
            str(_AGENT_B): {"web": "http://localhost:3002"},
            str(_AGENT_A): {"web": "http://localhost:3001"},
        },
    )
    ids = resolver.list_known_agent_ids()
    assert ids == (_AGENT_A, _AGENT_B)


def test_static_list_known_agent_ids_returns_empty_tuple_when_no_agents() -> None:
    resolver = StaticBackendResolver(url_by_agent_and_server={})
    ids = resolver.list_known_agent_ids()
    assert ids == ()


def test_static_list_servers_for_agent_returns_sorted_names() -> None:
    resolver = StaticBackendResolver(
        url_by_agent_and_server={
            str(_AGENT_A): {"web": "http://localhost:3001", "api": "http://localhost:3002"},
        },
    )
    servers = resolver.list_servers_for_agent(_AGENT_A)
    assert servers == (_SERVER_API, _SERVER_WEB)


def test_static_list_servers_for_agent_returns_empty_for_unknown_agent() -> None:
    resolver = StaticBackendResolver(url_by_agent_and_server={})
    servers = resolver.list_servers_for_agent(_AGENT_A)
    assert servers == ()


# -- parse_server_log_records tests --


def test_parse_server_log_records_parses_valid_jsonl() -> None:
    text = '{"server": "web", "url": "http://127.0.0.1:9100"}\n'
    records = parse_server_log_records(text)

    assert len(records) == 1
    assert records[0].server == ServerName("web")
    assert records[0].url == "http://127.0.0.1:9100"


def test_parse_server_log_records_returns_empty_for_empty_input() -> None:
    assert parse_server_log_records("") == []
    assert parse_server_log_records("\n") == []


def test_parse_server_log_records_raises_on_invalid_json() -> None:
    text = 'bad line\n{"server": "web", "url": "http://127.0.0.1:9100"}\n'
    with pytest.raises(json.JSONDecodeError):
        parse_server_log_records(text)


def test_parse_server_log_records_raises_on_missing_fields() -> None:
    text = '{"server": "web"}\n'
    with pytest.raises(ServerLogParseError, match="missing required fields"):
        parse_server_log_records(text)


def test_parse_server_log_records_ignores_envelope_fields() -> None:
    text = (
        json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00.000000000Z",
                "type": "server_registered",
                "event_id": "evt-abc123",
                "source": "servers",
                "server": "web",
                "url": "http://127.0.0.1:9100",
            }
        )
        + "\n"
    )
    records = parse_server_log_records(text)

    assert len(records) == 1
    assert records[0].server == ServerName("web")
    assert records[0].url == "http://127.0.0.1:9100"


def test_parse_server_log_records_returns_multiple_records() -> None:
    text = '{"server": "web", "url": "http://127.0.0.1:9100"}\n{"server": "api", "url": "http://127.0.0.1:9200"}\n'
    records = parse_server_log_records(text)

    assert len(records) == 2
    assert records[0].server == ServerName("web")
    assert records[1].server == ServerName("api")


# -- parse_agent_ids_from_json tests --


def test_parse_agent_ids_from_json_parses_valid_output() -> None:
    json_output = make_agents_json(_AGENT_A, _AGENT_B)
    ids = parse_agent_ids_from_json(json_output)

    assert _AGENT_A in ids
    assert _AGENT_B in ids


def test_parse_agent_ids_from_json_returns_empty_for_none() -> None:
    assert parse_agent_ids_from_json(None) == ()


def test_parse_agent_ids_from_json_returns_empty_for_invalid_json() -> None:
    assert parse_agent_ids_from_json("not json") == ()


# -- MngCliBackendResolver tests (using direct state updates) --


def test_mng_cli_resolver_returns_url_for_specific_server() -> None:
    resolver = make_resolver_with_data(
        server_logs={str(_AGENT_A): make_server_log("web", "http://127.0.0.1:9100")},
        agents_json=make_agents_json(_AGENT_A),
    )
    assert resolver.get_backend_url(_AGENT_A, _SERVER_WEB) == "http://127.0.0.1:9100"


def test_mng_cli_resolver_returns_none_for_unknown_server_name() -> None:
    resolver = make_resolver_with_data(
        server_logs={str(_AGENT_A): make_server_log("web", "http://127.0.0.1:9100")},
        agents_json=make_agents_json(_AGENT_A),
    )
    assert resolver.get_backend_url(_AGENT_A, _SERVER_API) is None


def test_mng_cli_resolver_returns_none_for_unknown_agent() -> None:
    resolver = make_resolver_with_data(server_logs={}, agents_json=make_agents_json())
    assert resolver.get_backend_url(_AGENT_A, _SERVER_WEB) is None


def test_mng_cli_resolver_handles_multiple_servers_for_one_agent() -> None:
    log_content = make_server_log("web", "http://127.0.0.1:9100") + make_server_log("api", "http://127.0.0.1:9200")
    resolver = make_resolver_with_data(
        server_logs={str(_AGENT_A): log_content},
        agents_json=make_agents_json(_AGENT_A),
    )
    assert resolver.get_backend_url(_AGENT_A, _SERVER_WEB) == "http://127.0.0.1:9100"
    assert resolver.get_backend_url(_AGENT_A, _SERVER_API) == "http://127.0.0.1:9200"


def test_mng_cli_resolver_later_entry_overrides_earlier_for_same_server() -> None:
    log_content = make_server_log("web", "http://127.0.0.1:9100") + make_server_log("web", "http://127.0.0.1:9200")
    resolver = make_resolver_with_data(
        server_logs={str(_AGENT_A): log_content},
        agents_json=make_agents_json(_AGENT_A),
    )
    assert resolver.get_backend_url(_AGENT_A, _SERVER_WEB) == "http://127.0.0.1:9200"


def test_mng_cli_resolver_lists_servers_for_agent() -> None:
    log_content = make_server_log("web", "http://127.0.0.1:9100") + make_server_log("api", "http://127.0.0.1:9200")
    resolver = make_resolver_with_data(
        server_logs={str(_AGENT_A): log_content},
        agents_json=make_agents_json(_AGENT_A),
    )
    servers = resolver.list_servers_for_agent(_AGENT_A)
    assert servers == (_SERVER_API, _SERVER_WEB)


def test_mng_cli_resolver_lists_known_agents() -> None:
    resolver = make_resolver_with_data(
        server_logs={},
        agents_json=make_agents_json(_AGENT_A, _AGENT_B),
    )
    ids = resolver.list_known_agent_ids()
    assert _AGENT_A in ids
    assert _AGENT_B in ids


def test_mng_cli_resolver_returns_empty_when_no_agents() -> None:
    resolver = make_resolver_with_data(server_logs={}, agents_json=make_agents_json())
    assert resolver.list_known_agent_ids() == ()


def test_mng_cli_resolver_returns_empty_when_no_data() -> None:
    resolver = MngCliBackendResolver()
    assert resolver.list_known_agent_ids() == ()


def test_mng_cli_resolver_update_agents_replaces_state() -> None:
    """Calling update_agents replaces the agent list and SSH info."""
    resolver = MngCliBackendResolver()

    resolver.update_agents(
        ParsedAgentsResult(agent_ids=(_AGENT_A, _AGENT_B)),
    )
    assert resolver.list_known_agent_ids() == (_AGENT_A, _AGENT_B)

    resolver.update_agents(
        ParsedAgentsResult(agent_ids=(_AGENT_A,)),
    )
    assert resolver.list_known_agent_ids() == (_AGENT_A,)


def test_mng_cli_resolver_update_servers_replaces_state() -> None:
    """Calling update_servers replaces the server map for that agent."""
    resolver = MngCliBackendResolver()

    resolver.update_servers(_AGENT_A, {"web": "http://127.0.0.1:9100"})
    assert resolver.get_backend_url(_AGENT_A, _SERVER_WEB) == "http://127.0.0.1:9100"

    resolver.update_servers(_AGENT_A, {"web": "http://127.0.0.1:9200"})
    assert resolver.get_backend_url(_AGENT_A, _SERVER_WEB) == "http://127.0.0.1:9200"


# -- parse_agents_from_json tests --


def _make_agents_json_with_ssh(*agents: tuple[str, Mapping[str, object] | None]) -> str:
    """Build mng list --format json output with optional SSH info per agent."""
    agent_list = []
    for agent_id, ssh in agents:
        agent: dict[str, object] = {"id": agent_id}
        if ssh is not None:
            agent["host"] = {"ssh": ssh}
        else:
            agent["host"] = {}
        agent_list.append(agent)
    return json.dumps({"agents": agent_list})


def test_parse_agents_from_json_extracts_agent_ids() -> None:
    json_str = _make_agents_json_with_ssh(
        (str(_AGENT_A), None),
        (str(_AGENT_B), None),
    )
    result = parse_agents_from_json(json_str)
    assert _AGENT_A in result.agent_ids
    assert _AGENT_B in result.agent_ids


def test_parse_agents_from_json_extracts_ssh_info() -> None:
    ssh_data = {
        "user": "root",
        "host": "remote.example.com",
        "port": 12345,
        "key_path": "/home/user/.mng/providers/modal/modal_ssh_key",
    }
    json_str = _make_agents_json_with_ssh((str(_AGENT_A), ssh_data))
    result = parse_agents_from_json(json_str)

    ssh_info = result.ssh_info_by_agent_id.get(str(_AGENT_A))
    assert ssh_info is not None
    assert ssh_info.user == "root"
    assert ssh_info.host == "remote.example.com"
    assert ssh_info.port == 12345
    assert ssh_info.key_path == Path("/home/user/.mng/providers/modal/modal_ssh_key")


def test_parse_agents_from_json_returns_none_ssh_for_local_agents() -> None:
    json_str = _make_agents_json_with_ssh((str(_AGENT_A), None))
    result = parse_agents_from_json(json_str)

    assert str(_AGENT_A) not in result.ssh_info_by_agent_id


def test_parse_agents_from_json_handles_mixed_local_and_remote() -> None:
    ssh_data = {
        "user": "root",
        "host": "remote.example.com",
        "port": 12345,
        "key_path": "/tmp/key",
    }
    json_str = _make_agents_json_with_ssh(
        (str(_AGENT_A), None),
        (str(_AGENT_B), ssh_data),
    )
    result = parse_agents_from_json(json_str)

    assert len(result.agent_ids) == 2
    assert str(_AGENT_A) not in result.ssh_info_by_agent_id
    assert str(_AGENT_B) in result.ssh_info_by_agent_id


def test_parse_agents_from_json_returns_empty_for_none() -> None:
    result = parse_agents_from_json(None)
    assert result.agent_ids == ()
    assert result.ssh_info_by_agent_id == {}


def test_parse_agents_from_json_returns_empty_for_invalid_json() -> None:
    result = parse_agents_from_json("not json")
    assert result.agent_ids == ()


def test_parse_agents_from_json_skips_agents_with_invalid_ssh() -> None:
    json_str = json.dumps(
        {
            "agents": [
                {
                    "id": str(_AGENT_A),
                    "host": {"ssh": {"user": "root"}},
                },
            ],
        }
    )
    result = parse_agents_from_json(json_str)
    assert _AGENT_A in result.agent_ids
    assert str(_AGENT_A) not in result.ssh_info_by_agent_id


# -- MngCliBackendResolver.get_ssh_info tests --


def test_mng_cli_resolver_get_ssh_info_returns_info_for_remote_agent() -> None:
    ssh_data = {
        "user": "root",
        "host": "remote.example.com",
        "port": 12345,
        "key_path": "/tmp/test_key",
    }
    agents_json = _make_agents_json_with_ssh((str(_AGENT_A), ssh_data))
    resolver = make_resolver_with_data(server_logs={}, agents_json=agents_json)

    ssh_info = resolver.get_ssh_info(_AGENT_A)
    assert ssh_info is not None
    assert ssh_info.host == "remote.example.com"
    assert ssh_info.port == 12345


def test_mng_cli_resolver_get_ssh_info_returns_none_for_local_agent() -> None:
    agents_json = _make_agents_json_with_ssh((str(_AGENT_A), None))
    resolver = make_resolver_with_data(server_logs={}, agents_json=agents_json)

    assert resolver.get_ssh_info(_AGENT_A) is None


def test_mng_cli_resolver_get_ssh_info_returns_none_for_unknown_agent() -> None:
    agents_json = _make_agents_json_with_ssh((str(_AGENT_A), None))
    resolver = make_resolver_with_data(server_logs={}, agents_json=agents_json)

    assert resolver.get_ssh_info(_AGENT_B) is None


# -- BackendResolverInterface.get_ssh_info default --


def test_backend_resolver_interface_default_get_ssh_info_returns_none() -> None:
    """The base class default get_ssh_info returns None for all agents."""

    class MinimalResolver(BackendResolverInterface):
        def get_backend_url(self, agent_id: AgentId, server_name: ServerName) -> str | None:
            return None

        def list_known_agent_ids(self) -> tuple[AgentId, ...]:
            return ()

        def list_servers_for_agent(self, agent_id: AgentId) -> tuple[ServerName, ...]:
            return ()

    resolver = MinimalResolver()
    assert resolver.get_ssh_info(_AGENT_A) is None


# -- MngStreamManager tests (calling methods directly, no subprocesses) --


def _make_stream_manager() -> MngStreamManager:
    """Create a MngStreamManager with a fresh resolver, without starting subprocesses."""
    resolver = MngCliBackendResolver()
    return MngStreamManager(resolver=resolver)


def test_stream_manager_on_list_stream_output_ignores_stderr() -> None:
    manager = _make_stream_manager()
    manager._on_list_stream_output("some stderr line", is_stdout=False)
    assert manager.resolver.list_known_agent_ids() == ()


def test_stream_manager_on_list_stream_output_ignores_empty_lines() -> None:
    manager = _make_stream_manager()
    manager._on_list_stream_output("", is_stdout=True)
    manager._on_list_stream_output("  \n", is_stdout=True)
    assert manager.resolver.list_known_agent_ids() == ()


def test_stream_manager_on_list_stream_output_ignores_non_full_events() -> None:
    """Non-DISCOVERY_FULL events are ignored and do not update the resolver."""
    manager = _make_stream_manager()
    # Use an unrecognized event type so parse_discovery_event_line returns None
    line = json.dumps(
        {
            "type": "SOME_OTHER_EVENT",
            "timestamp": "2026-01-01T00:00:00Z",
            "event_id": "evt-test-001",
            "source": "mng/discovery",
        }
    )
    manager._on_list_stream_output(line, is_stdout=True)
    assert manager.resolver.list_known_agent_ids() == ()


def test_stream_manager_handle_discovery_line_ignores_invalid_json() -> None:
    manager = _make_stream_manager()
    manager._handle_discovery_line("not valid json {{{")
    assert manager.resolver.list_known_agent_ids() == ()


def test_stream_manager_on_events_stream_output_updates_servers() -> None:
    manager = _make_stream_manager()
    manager._events_servers[str(_AGENT_A)] = {}

    server_line = json.dumps({"server": "web", "url": "http://127.0.0.1:9100"})
    manager._on_events_stream_output(server_line, is_stdout=True, agent_id=_AGENT_A)

    assert manager.resolver.get_backend_url(_AGENT_A, _SERVER_WEB) == "http://127.0.0.1:9100"


def test_stream_manager_on_events_stream_output_ignores_stderr() -> None:
    manager = _make_stream_manager()
    manager._events_servers[str(_AGENT_A)] = {}

    manager._on_events_stream_output("stderr noise", is_stdout=False, agent_id=_AGENT_A)
    assert manager.resolver.get_backend_url(_AGENT_A, _SERVER_WEB) is None


def test_stream_manager_on_events_stream_output_ignores_invalid_json() -> None:
    manager = _make_stream_manager()
    manager._events_servers[str(_AGENT_A)] = {}

    manager._on_events_stream_output("not json", is_stdout=True, agent_id=_AGENT_A)
    assert manager.resolver.get_backend_url(_AGENT_A, _SERVER_WEB) is None


def test_stream_manager_on_events_stream_output_accumulates_servers() -> None:
    """Multiple server records for the same agent accumulate in the server map."""
    manager = _make_stream_manager()
    manager._events_servers[str(_AGENT_A)] = {}

    web_line = json.dumps({"server": "web", "url": "http://127.0.0.1:9100"})
    api_line = json.dumps({"server": "api", "url": "http://127.0.0.1:9200"})

    manager._on_events_stream_output(web_line, is_stdout=True, agent_id=_AGENT_A)
    manager._on_events_stream_output(api_line, is_stdout=True, agent_id=_AGENT_A)

    assert manager.resolver.get_backend_url(_AGENT_A, _SERVER_WEB) == "http://127.0.0.1:9100"
    assert manager.resolver.get_backend_url(_AGENT_A, _SERVER_API) == "http://127.0.0.1:9200"


def test_stream_manager_on_events_stream_output_later_entry_overrides_earlier() -> None:
    """A later server record for the same server name replaces the earlier URL."""
    manager = _make_stream_manager()
    manager._events_servers[str(_AGENT_A)] = {}

    line1 = json.dumps({"server": "web", "url": "http://127.0.0.1:9100"})
    line2 = json.dumps({"server": "web", "url": "http://127.0.0.1:9200"})

    manager._on_events_stream_output(line1, is_stdout=True, agent_id=_AGENT_A)
    manager._on_events_stream_output(line2, is_stdout=True, agent_id=_AGENT_A)

    assert manager.resolver.get_backend_url(_AGENT_A, _SERVER_WEB) == "http://127.0.0.1:9200"


def _make_discovery_full_line(
    agents: list[tuple[str, str]],
    hosts: list[str],
) -> str:
    """Build a DISCOVERY_FULL event JSON line.

    agents: list of (agent_id, host_id) tuples.
    hosts: list of host_id strings.
    """
    return json.dumps(
        {
            "type": "DISCOVERY_FULL",
            "timestamp": "2026-01-01T00:00:00Z",
            "event_id": "evt-test-full-001",
            "source": "mng/discovery",
            "agents": [
                {
                    "host_id": host_id,
                    "agent_id": agent_id,
                    "agent_name": f"agent-{agent_id[-4:]}",
                    "provider_name": "modal",
                    "certified_data": {},
                }
                for agent_id, host_id in agents
            ],
            "hosts": [
                {
                    "host_id": host_id,
                    "host_name": f"host-{host_id[-4:]}",
                    "provider_name": "modal",
                }
                for host_id in hosts
            ],
        }
    )


def _make_host_ssh_info_line(host_id: str, ssh_data: Mapping[str, object]) -> str:
    """Build a HOST_SSH_INFO event JSON line."""
    return json.dumps(
        {
            "type": "HOST_SSH_INFO",
            "timestamp": "2026-01-01T00:00:01Z",
            "event_id": "evt-test-ssh-001",
            "source": "mng/discovery",
            "host_id": host_id,
            "ssh": ssh_data,
        }
    )


def test_stream_manager_full_snapshot_updates_agent_ids() -> None:
    """DISCOVERY_FULL events update the agent list in the resolver."""
    manager = _make_stream_manager()
    host_id = "host-00000000000000000000000000000001"
    line = _make_discovery_full_line(
        agents=[(str(_AGENT_A), host_id), (str(_AGENT_B), host_id)],
        hosts=[host_id],
    )
    with manager._cg:
        manager._handle_discovery_line(line)

    ids = manager.resolver.list_known_agent_ids()
    assert _AGENT_A in ids
    assert _AGENT_B in ids


def test_stream_manager_host_ssh_info_populates_resolver() -> None:
    """HOST_SSH_INFO events followed by agent mappings populate SSH info."""
    manager = _make_stream_manager()
    host_id = "host-00000000000000000000000000000001"
    ssh_data = {
        "user": "root",
        "host": "remote.example.com",
        "port": 2222,
        "key_path": "/tmp/test_key",
        "command": "ssh -i /tmp/test_key -p 2222 root@remote.example.com",
    }

    with manager._cg:
        # First, establish agent-to-host mapping via DISCOVERY_FULL
        full_line = _make_discovery_full_line(
            agents=[(str(_AGENT_A), host_id)],
            hosts=[host_id],
        )
        manager._handle_discovery_line(full_line)

        # Then receive SSH info for the host
        ssh_line = _make_host_ssh_info_line(host_id, ssh_data)
        manager._handle_discovery_line(ssh_line)

    ssh_info = manager.resolver.get_ssh_info(_AGENT_A)
    assert ssh_info is not None
    assert ssh_info.host == "remote.example.com"
    assert ssh_info.port == 2222
    assert ssh_info.key_path == Path("/tmp/test_key")


def test_stream_manager_no_ssh_for_local_hosts() -> None:
    """Agents on hosts without SSH info return None."""
    manager = _make_stream_manager()
    host_id = "host-00000000000000000000000000000001"

    with manager._cg:
        line = _make_discovery_full_line(
            agents=[(str(_AGENT_A), host_id)],
            hosts=[host_id],
        )
        manager._handle_discovery_line(line)

    assert manager.resolver.list_known_agent_ids() == (_AGENT_A,)
    assert manager.resolver.get_ssh_info(_AGENT_A) is None


def test_stream_manager_mixed_local_and_remote() -> None:
    """Agents on different hosts get correct SSH info (or None for local)."""
    manager = _make_stream_manager()
    local_host_id = "host-00000000000000000000000000000001"
    remote_host_id = "host-00000000000000000000000000000002"
    ssh_data = {
        "user": "root",
        "host": "remote.example.com",
        "port": 2222,
        "key_path": "/tmp/key",
        "command": "ssh -i /tmp/key -p 2222 root@remote.example.com",
    }

    with manager._cg:
        full_line = _make_discovery_full_line(
            agents=[(str(_AGENT_A), local_host_id), (str(_AGENT_B), remote_host_id)],
            hosts=[local_host_id, remote_host_id],
        )
        manager._handle_discovery_line(full_line)

        ssh_line = _make_host_ssh_info_line(remote_host_id, ssh_data)
        manager._handle_discovery_line(ssh_line)

    assert manager.resolver.get_ssh_info(_AGENT_A) is None
    ssh_info = manager.resolver.get_ssh_info(_AGENT_B)
    assert ssh_info is not None
    assert ssh_info.host == "remote.example.com"


def test_stream_manager_ssh_info_before_full_snapshot() -> None:
    """SSH info received before DISCOVERY_FULL is retained and used."""
    manager = _make_stream_manager()
    host_id = "host-00000000000000000000000000000001"
    ssh_data = {
        "user": "root",
        "host": "remote.example.com",
        "port": 2222,
        "key_path": "/tmp/key",
        "command": "ssh -i /tmp/key -p 2222 root@remote.example.com",
    }

    with manager._cg:
        # SSH info arrives first
        ssh_line = _make_host_ssh_info_line(host_id, ssh_data)
        manager._handle_discovery_line(ssh_line)

        # Then the full snapshot maps the agent to that host
        full_line = _make_discovery_full_line(
            agents=[(str(_AGENT_A), host_id)],
            hosts=[host_id],
        )
        manager._handle_discovery_line(full_line)

    ssh_info = manager.resolver.get_ssh_info(_AGENT_A)
    assert ssh_info is not None
    assert ssh_info.host == "remote.example.com"
