import json
from pathlib import Path

from imbue.changelings.forwarding_server.backend_resolver import BackendResolverInterface
from imbue.changelings.forwarding_server.backend_resolver import MngCliBackendResolver
from imbue.changelings.forwarding_server.backend_resolver import MngCliInterface
from imbue.changelings.forwarding_server.backend_resolver import StaticBackendResolver
from imbue.changelings.forwarding_server.backend_resolver import _parse_agent_ids_from_json
from imbue.changelings.forwarding_server.backend_resolver import _parse_agents_from_json
from imbue.changelings.forwarding_server.backend_resolver import _parse_server_log_records
from imbue.changelings.forwarding_server.conftest import FakeMngCli
from imbue.changelings.forwarding_server.conftest import make_agents_json
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


# -- _parse_server_log_records tests --


def test_parse_server_log_records_parses_valid_jsonl() -> None:
    text = '{"server": "web", "url": "http://127.0.0.1:9100"}\n'
    records = _parse_server_log_records(text)

    assert len(records) == 1
    assert records[0].server == ServerName("web")
    assert records[0].url == "http://127.0.0.1:9100"


def test_parse_server_log_records_returns_empty_for_empty_input() -> None:
    assert _parse_server_log_records("") == []
    assert _parse_server_log_records("\n") == []


def test_parse_server_log_records_skips_invalid_lines() -> None:
    text = 'bad line\n{"server": "web", "url": "http://127.0.0.1:9100"}\n'
    records = _parse_server_log_records(text)

    assert len(records) == 1
    assert records[0].url == "http://127.0.0.1:9100"


def test_parse_server_log_records_returns_multiple_records() -> None:
    text = '{"server": "web", "url": "http://127.0.0.1:9100"}\n{"server": "api", "url": "http://127.0.0.1:9200"}\n'
    records = _parse_server_log_records(text)

    assert len(records) == 2
    assert records[0].server == ServerName("web")
    assert records[1].server == ServerName("api")


# -- _parse_agent_ids_from_json tests --


def test_parse_agent_ids_from_json_parses_valid_output() -> None:
    json_output = make_agents_json(_AGENT_A, _AGENT_B)
    ids = _parse_agent_ids_from_json(json_output)

    assert _AGENT_A in ids
    assert _AGENT_B in ids


def test_parse_agent_ids_from_json_returns_empty_for_none() -> None:
    assert _parse_agent_ids_from_json(None) == ()


def test_parse_agent_ids_from_json_returns_empty_for_invalid_json() -> None:
    assert _parse_agent_ids_from_json("not json") == ()


# -- MngCliBackendResolver tests (using FakeMngCli) --


def test_mng_cli_resolver_returns_url_for_specific_server() -> None:
    fake_cli = FakeMngCli(
        server_logs={str(_AGENT_A): make_server_log("web", "http://127.0.0.1:9100")},
        agents_json=make_agents_json(_AGENT_A),
    )
    resolver = MngCliBackendResolver(mng_cli=fake_cli)

    assert resolver.get_backend_url(_AGENT_A, _SERVER_WEB) == "http://127.0.0.1:9100"


def test_mng_cli_resolver_returns_none_for_unknown_server_name() -> None:
    fake_cli = FakeMngCli(
        server_logs={str(_AGENT_A): make_server_log("web", "http://127.0.0.1:9100")},
        agents_json=make_agents_json(_AGENT_A),
    )
    resolver = MngCliBackendResolver(mng_cli=fake_cli)

    assert resolver.get_backend_url(_AGENT_A, _SERVER_API) is None


def test_mng_cli_resolver_returns_none_for_unknown_agent() -> None:
    fake_cli = FakeMngCli(server_logs={}, agents_json=make_agents_json())
    resolver = MngCliBackendResolver(mng_cli=fake_cli)

    assert resolver.get_backend_url(_AGENT_A, _SERVER_WEB) is None


def test_mng_cli_resolver_handles_multiple_servers_for_one_agent() -> None:
    log_content = make_server_log("web", "http://127.0.0.1:9100") + make_server_log("api", "http://127.0.0.1:9200")
    fake_cli = FakeMngCli(
        server_logs={str(_AGENT_A): log_content},
        agents_json=make_agents_json(_AGENT_A),
    )
    resolver = MngCliBackendResolver(mng_cli=fake_cli)

    assert resolver.get_backend_url(_AGENT_A, _SERVER_WEB) == "http://127.0.0.1:9100"
    assert resolver.get_backend_url(_AGENT_A, _SERVER_API) == "http://127.0.0.1:9200"


def test_mng_cli_resolver_later_entry_overrides_earlier_for_same_server() -> None:
    log_content = make_server_log("web", "http://127.0.0.1:9100") + make_server_log("web", "http://127.0.0.1:9200")
    fake_cli = FakeMngCli(
        server_logs={str(_AGENT_A): log_content},
        agents_json=make_agents_json(_AGENT_A),
    )
    resolver = MngCliBackendResolver(mng_cli=fake_cli)

    assert resolver.get_backend_url(_AGENT_A, _SERVER_WEB) == "http://127.0.0.1:9200"


def test_mng_cli_resolver_lists_servers_for_agent() -> None:
    log_content = make_server_log("web", "http://127.0.0.1:9100") + make_server_log("api", "http://127.0.0.1:9200")
    fake_cli = FakeMngCli(
        server_logs={str(_AGENT_A): log_content},
        agents_json=make_agents_json(_AGENT_A),
    )
    resolver = MngCliBackendResolver(mng_cli=fake_cli)

    servers = resolver.list_servers_for_agent(_AGENT_A)
    assert servers == (_SERVER_API, _SERVER_WEB)


def test_mng_cli_resolver_lists_known_agents() -> None:
    fake_cli = FakeMngCli(
        server_logs={},
        agents_json=make_agents_json(_AGENT_A, _AGENT_B),
    )
    resolver = MngCliBackendResolver(mng_cli=fake_cli)
    ids = resolver.list_known_agent_ids()

    assert _AGENT_A in ids
    assert _AGENT_B in ids


def test_mng_cli_resolver_returns_empty_when_no_agents() -> None:
    fake_cli = FakeMngCli(server_logs={}, agents_json=make_agents_json())
    resolver = MngCliBackendResolver(mng_cli=fake_cli)

    assert resolver.list_known_agent_ids() == ()


def test_mng_cli_resolver_returns_empty_when_mng_list_fails() -> None:
    fake_cli = FakeMngCli(server_logs={}, agents_json=None)
    resolver = MngCliBackendResolver(mng_cli=fake_cli)

    assert resolver.list_known_agent_ids() == ()


class _CountingMngCli(MngCliInterface):
    """MngCliInterface that counts how many times read_agent_log is called."""

    server_logs: dict[str, str]
    agents_json: str | None = None
    read_count: int = 0

    def read_agent_log(self, agent_id: AgentId, log_file: str) -> str | None:
        self.read_count += 1
        return self.server_logs.get(str(agent_id))

    def list_agents_json(self) -> str | None:
        return self.agents_json


def test_mng_cli_resolver_caches_server_resolution() -> None:
    fake_cli = _CountingMngCli(
        server_logs={str(_AGENT_A): make_server_log("web", "http://127.0.0.1:9100")},
    )
    resolver = MngCliBackendResolver(mng_cli=fake_cli)

    url1 = resolver.get_backend_url(_AGENT_A, _SERVER_WEB)
    url2 = resolver.get_backend_url(_AGENT_A, _SERVER_WEB)

    assert url1 == "http://127.0.0.1:9100"
    assert url2 == "http://127.0.0.1:9100"
    assert fake_cli.read_count == 1


def test_mng_cli_resolver_cache_serves_multiple_servers_from_single_fetch() -> None:
    """After resolving servers for an agent, all server lookups for that agent use the cache."""
    log_content = make_server_log("web", "http://127.0.0.1:9100") + make_server_log("api", "http://127.0.0.1:9200")
    fake_cli = _CountingMngCli(server_logs={str(_AGENT_A): log_content})
    resolver = MngCliBackendResolver(mng_cli=fake_cli)

    web_url = resolver.get_backend_url(_AGENT_A, _SERVER_WEB)
    api_url = resolver.get_backend_url(_AGENT_A, _SERVER_API)

    assert web_url == "http://127.0.0.1:9100"
    assert api_url == "http://127.0.0.1:9200"
    assert fake_cli.read_count == 1


# -- _parse_agents_from_json tests --


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


def test_parse_agents_from_json_extracts_agent_ids() -> None:
    json_str = _make_agents_json_with_ssh(
        (str(_AGENT_A), None),
        (str(_AGENT_B), None),
    )
    result = _parse_agents_from_json(json_str)
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
    result = _parse_agents_from_json(json_str)

    ssh_info = result.ssh_info_by_agent_id.get(str(_AGENT_A))
    assert ssh_info is not None
    assert ssh_info.user == "root"
    assert ssh_info.host == "remote.example.com"
    assert ssh_info.port == 12345
    assert ssh_info.key_path == Path("/home/user/.mng/providers/modal/modal_ssh_key")


def test_parse_agents_from_json_returns_none_ssh_for_local_agents() -> None:
    json_str = _make_agents_json_with_ssh((str(_AGENT_A), None))
    result = _parse_agents_from_json(json_str)

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
    result = _parse_agents_from_json(json_str)

    assert len(result.agent_ids) == 2
    assert str(_AGENT_A) not in result.ssh_info_by_agent_id
    assert str(_AGENT_B) in result.ssh_info_by_agent_id


def test_parse_agents_from_json_returns_empty_for_none() -> None:
    result = _parse_agents_from_json(None)
    assert result.agent_ids == ()
    assert result.ssh_info_by_agent_id == {}


def test_parse_agents_from_json_returns_empty_for_invalid_json() -> None:
    result = _parse_agents_from_json("not json")
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
    result = _parse_agents_from_json(json_str)
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
    fake_cli = FakeMngCli(server_logs={}, agents_json=agents_json)
    resolver = MngCliBackendResolver(mng_cli=fake_cli)

    ssh_info = resolver.get_ssh_info(_AGENT_A)
    assert ssh_info is not None
    assert ssh_info.host == "remote.example.com"
    assert ssh_info.port == 12345


def test_mng_cli_resolver_get_ssh_info_returns_none_for_local_agent() -> None:
    agents_json = _make_agents_json_with_ssh((str(_AGENT_A), None))
    fake_cli = FakeMngCli(server_logs={}, agents_json=agents_json)
    resolver = MngCliBackendResolver(mng_cli=fake_cli)

    assert resolver.get_ssh_info(_AGENT_A) is None


def test_mng_cli_resolver_get_ssh_info_returns_none_for_unknown_agent() -> None:
    agents_json = _make_agents_json_with_ssh((str(_AGENT_A), None))
    fake_cli = FakeMngCli(server_logs={}, agents_json=agents_json)
    resolver = MngCliBackendResolver(mng_cli=fake_cli)

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
