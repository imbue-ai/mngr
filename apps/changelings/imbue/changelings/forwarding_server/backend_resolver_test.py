import json
from pathlib import Path

from imbue.changelings.forwarding_server.backend_resolver import BackendResolverInterface
from imbue.changelings.forwarding_server.backend_resolver import MngCliBackendResolver
from imbue.changelings.forwarding_server.backend_resolver import StaticBackendResolver
from imbue.changelings.forwarding_server.backend_resolver import ParsedAgentsResult
from imbue.changelings.forwarding_server.backend_resolver import parse_agent_ids_from_json
from imbue.changelings.forwarding_server.backend_resolver import parse_agents_from_json
from imbue.changelings.forwarding_server.backend_resolver import parse_server_log_records
from imbue.changelings.forwarding_server.conftest import make_agents_json
from imbue.changelings.forwarding_server.conftest import make_resolver_with_data
from imbue.changelings.forwarding_server.conftest import make_server_log
from imbue.changelings.primitives import ServerName
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


def testparse_server_log_records_parses_valid_jsonl() -> None:
    text = '{"server": "web", "url": "http://127.0.0.1:9100"}\n'
    records = parse_server_log_records(text)

    assert len(records) == 1
    assert records[0].server == ServerName("web")
    assert records[0].url == "http://127.0.0.1:9100"


def testparse_server_log_records_returns_empty_for_empty_input() -> None:
    assert parse_server_log_records("") == []
    assert parse_server_log_records("\n") == []


def testparse_server_log_records_skips_invalid_lines() -> None:
    text = 'bad line\n{"server": "web", "url": "http://127.0.0.1:9100"}\n'
    records = parse_server_log_records(text)

    assert len(records) == 1
    assert records[0].url == "http://127.0.0.1:9100"


def testparse_server_log_records_returns_multiple_records() -> None:
    text = '{"server": "web", "url": "http://127.0.0.1:9100"}\n{"server": "api", "url": "http://127.0.0.1:9200"}\n'
    records = parse_server_log_records(text)

    assert len(records) == 2
    assert records[0].server == ServerName("web")
    assert records[1].server == ServerName("api")


# -- parse_agent_ids_from_json tests --


def testparse_agent_ids_from_json_parses_valid_output() -> None:
    json_output = make_agents_json(_AGENT_A, _AGENT_B)
    ids = parse_agent_ids_from_json(json_output)

    assert _AGENT_A in ids
    assert _AGENT_B in ids


def testparse_agent_ids_from_json_returns_empty_for_none() -> None:
    assert parse_agent_ids_from_json(None) == ()


def testparse_agent_ids_from_json_returns_empty_for_invalid_json() -> None:
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


def _make_agents_json_with_ssh(*agents: tuple[str, dict[str, object] | None]) -> str:
    """Build mng list --json output with optional SSH info per agent."""
    agent_list = []
    for agent_id, ssh in agents:
        agent: dict[str, object] = {"id": agent_id}
        if ssh is not None:
            agent["host"] = {"ssh": ssh}
        else:
            agent["host"] = {}
        agent_list.append(agent)
    return json.dumps({"agents": agent_list})


def testparse_agents_from_json_extracts_agent_ids() -> None:
    json_str = _make_agents_json_with_ssh(
        (str(_AGENT_A), None),
        (str(_AGENT_B), None),
    )
    result = parse_agents_from_json(json_str)
    assert _AGENT_A in result.agent_ids
    assert _AGENT_B in result.agent_ids


def testparse_agents_from_json_extracts_ssh_info() -> None:
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


def testparse_agents_from_json_returns_none_ssh_for_local_agents() -> None:
    json_str = _make_agents_json_with_ssh((str(_AGENT_A), None))
    result = parse_agents_from_json(json_str)

    assert str(_AGENT_A) not in result.ssh_info_by_agent_id


def testparse_agents_from_json_handles_mixed_local_and_remote() -> None:
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


def testparse_agents_from_json_returns_empty_for_none() -> None:
    result = parse_agents_from_json(None)
    assert result.agent_ids == ()
    assert result.ssh_info_by_agent_id == {}


def testparse_agents_from_json_returns_empty_for_invalid_json() -> None:
    result = parse_agents_from_json("not json")
    assert result.agent_ids == ()


def testparse_agents_from_json_skips_agents_with_invalid_ssh() -> None:
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
