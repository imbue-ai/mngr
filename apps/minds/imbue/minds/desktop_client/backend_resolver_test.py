import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from loguru import logger as loguru_logger

from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import ServiceLogParseError
from imbue.minds.desktop_client.backend_resolver import ServiceLogRecord
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.backend_resolver import parse_agent_ids_from_json
from imbue.minds.desktop_client.backend_resolver import parse_agents_from_json
from imbue.minds.desktop_client.backend_resolver import parse_service_log_records
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_service_log
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.primitives import ServiceName
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName

_AGENT_A: AgentId = AgentId("agent-00000000000000000000000000000001")
_AGENT_B: AgentId = AgentId("agent-00000000000000000000000000000002")
_SERVICE_WEB: ServiceName = ServiceName("web")
_SERVICE_API: ServiceName = ServiceName("api")


# -- StaticBackendResolver tests --


def test_static_get_backend_url_returns_url_for_known_agent_and_service() -> None:
    resolver = StaticBackendResolver(
        url_by_agent_and_service={str(_AGENT_A): {"web": "http://localhost:3001"}},
    )
    url = resolver.get_backend_url(_AGENT_A, _SERVICE_WEB)
    assert url == "http://localhost:3001"


def test_static_get_backend_url_returns_none_for_unknown_agent() -> None:
    resolver = StaticBackendResolver(
        url_by_agent_and_service={str(_AGENT_A): {"web": "http://localhost:3001"}},
    )
    url = resolver.get_backend_url(_AGENT_B, _SERVICE_WEB)
    assert url is None


def test_static_get_backend_url_returns_none_for_unknown_service() -> None:
    resolver = StaticBackendResolver(
        url_by_agent_and_service={str(_AGENT_A): {"web": "http://localhost:3001"}},
    )
    url = resolver.get_backend_url(_AGENT_A, _SERVICE_API)
    assert url is None


def test_static_list_known_agent_ids_returns_sorted_ids() -> None:
    resolver = StaticBackendResolver(
        url_by_agent_and_service={
            str(_AGENT_B): {"web": "http://localhost:3002"},
            str(_AGENT_A): {"web": "http://localhost:3001"},
        },
    )
    ids = resolver.list_known_agent_ids()
    assert ids == (_AGENT_A, _AGENT_B)


def test_static_list_known_agent_ids_returns_empty_tuple_when_no_agents() -> None:
    resolver = StaticBackendResolver(url_by_agent_and_service={})
    ids = resolver.list_known_agent_ids()
    assert ids == ()


def test_static_list_services_for_agent_returns_sorted_names() -> None:
    resolver = StaticBackendResolver(
        url_by_agent_and_service={
            str(_AGENT_A): {"web": "http://localhost:3001", "api": "http://localhost:3002"},
        },
    )
    servers = resolver.list_services_for_agent(_AGENT_A)
    assert servers == (_SERVICE_API, _SERVICE_WEB)


def test_static_list_services_for_agent_returns_empty_for_unknown_agent() -> None:
    resolver = StaticBackendResolver(url_by_agent_and_service={})
    servers = resolver.list_services_for_agent(_AGENT_A)
    assert servers == ()


# -- parse_service_log_records tests --


def test_parse_service_log_records_parses_valid_jsonl() -> None:
    text = '{"service": "web", "url": "http://127.0.0.1:9100"}\n'
    records = parse_service_log_records(text)

    assert len(records) == 1
    assert isinstance(records[0], ServiceLogRecord)
    assert records[0].service == ServiceName("web")
    assert records[0].url == "http://127.0.0.1:9100"


def test_parse_service_log_records_returns_empty_for_empty_input() -> None:
    assert parse_service_log_records("") == []
    assert parse_service_log_records("\n") == []


def test_parse_service_log_records_raises_on_invalid_json() -> None:
    text = 'bad line\n{"service": "web", "url": "http://127.0.0.1:9100"}\n'
    with pytest.raises(json.JSONDecodeError):
        parse_service_log_records(text)


def test_parse_service_log_records_raises_on_missing_fields() -> None:
    text = '{"service": "web"}\n'
    with pytest.raises(ServiceLogParseError, match="missing required fields"):
        parse_service_log_records(text)


def test_parse_service_log_records_ignores_envelope_fields() -> None:
    text = (
        json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00.000000000Z",
                "type": "service_registered",
                "event_id": "evt-abc123",
                "source": "services",
                "service": "web",
                "url": "http://127.0.0.1:9100",
            }
        )
        + "\n"
    )
    records = parse_service_log_records(text)

    assert len(records) == 1
    assert isinstance(records[0], ServiceLogRecord)
    assert records[0].service == ServiceName("web")
    assert records[0].url == "http://127.0.0.1:9100"


def test_parse_service_log_records_returns_multiple_records() -> None:
    text = '{"service": "web", "url": "http://127.0.0.1:9100"}\n{"service": "api", "url": "http://127.0.0.1:9200"}\n'
    records = parse_service_log_records(text)

    assert len(records) == 2
    assert records[0].service == ServiceName("web")
    assert records[1].service == ServiceName("api")


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


# -- MngrCliBackendResolver tests (using direct state updates) --


def test_mngr_cli_resolver_returns_url_for_specific_service() -> None:
    resolver = make_resolver_with_data(
        service_logs={str(_AGENT_A): make_service_log("web", "http://127.0.0.1:9100")},
        agents_json=make_agents_json(_AGENT_A),
    )
    assert resolver.get_backend_url(_AGENT_A, _SERVICE_WEB) == "http://127.0.0.1:9100"


def test_mngr_cli_resolver_returns_none_for_unknown_service_name() -> None:
    resolver = make_resolver_with_data(
        service_logs={str(_AGENT_A): make_service_log("web", "http://127.0.0.1:9100")},
        agents_json=make_agents_json(_AGENT_A),
    )
    assert resolver.get_backend_url(_AGENT_A, _SERVICE_API) is None


def test_mngr_cli_resolver_returns_none_for_unknown_agent() -> None:
    resolver = make_resolver_with_data(service_logs={}, agents_json=make_agents_json())
    assert resolver.get_backend_url(_AGENT_A, _SERVICE_WEB) is None


def test_mngr_cli_resolver_handles_multiple_services_for_one_agent() -> None:
    log_content = make_service_log("web", "http://127.0.0.1:9100") + make_service_log("api", "http://127.0.0.1:9200")
    resolver = make_resolver_with_data(
        service_logs={str(_AGENT_A): log_content},
        agents_json=make_agents_json(_AGENT_A),
    )
    assert resolver.get_backend_url(_AGENT_A, _SERVICE_WEB) == "http://127.0.0.1:9100"
    assert resolver.get_backend_url(_AGENT_A, _SERVICE_API) == "http://127.0.0.1:9200"


def test_mngr_cli_resolver_later_entry_overrides_earlier_for_same_service() -> None:
    log_content = make_service_log("web", "http://127.0.0.1:9100") + make_service_log("web", "http://127.0.0.1:9200")
    resolver = make_resolver_with_data(
        service_logs={str(_AGENT_A): log_content},
        agents_json=make_agents_json(_AGENT_A),
    )
    assert resolver.get_backend_url(_AGENT_A, _SERVICE_WEB) == "http://127.0.0.1:9200"


def test_mngr_cli_resolver_lists_services_for_agent() -> None:
    log_content = make_service_log("web", "http://127.0.0.1:9100") + make_service_log("api", "http://127.0.0.1:9200")
    resolver = make_resolver_with_data(
        service_logs={str(_AGENT_A): log_content},
        agents_json=make_agents_json(_AGENT_A),
    )
    servers = resolver.list_services_for_agent(_AGENT_A)
    assert servers == (_SERVICE_API, _SERVICE_WEB)


def test_mngr_cli_resolver_lists_known_agents() -> None:
    resolver = make_resolver_with_data(
        service_logs={},
        agents_json=make_agents_json(_AGENT_A, _AGENT_B),
    )
    ids = resolver.list_known_agent_ids()
    assert _AGENT_A in ids
    assert _AGENT_B in ids


def test_mngr_cli_resolver_returns_empty_when_no_agents() -> None:
    resolver = make_resolver_with_data(service_logs={}, agents_json=make_agents_json())
    assert resolver.list_known_agent_ids() == ()


def test_mngr_cli_resolver_returns_empty_when_no_data() -> None:
    resolver = MngrCliBackendResolver()
    assert resolver.list_known_agent_ids() == ()


def test_mngr_cli_resolver_update_agents_replaces_state() -> None:
    """Calling update_agents replaces the agent list and SSH info."""
    resolver = MngrCliBackendResolver()

    resolver.update_agents(
        ParsedAgentsResult(agent_ids=(_AGENT_A, _AGENT_B)),
    )
    assert resolver.list_known_agent_ids() == (_AGENT_A, _AGENT_B)

    resolver.update_agents(
        ParsedAgentsResult(agent_ids=(_AGENT_A,)),
    )
    assert resolver.list_known_agent_ids() == (_AGENT_A,)


def test_mngr_cli_resolver_has_completed_initial_discovery() -> None:
    """has_completed_initial_discovery returns False until update_agents is called."""
    resolver = MngrCliBackendResolver()
    assert not resolver.has_completed_initial_discovery()

    resolver.update_agents(ParsedAgentsResult(agent_ids=()))
    assert resolver.has_completed_initial_discovery()


def _discovered_agent(host_id: HostId, agent_id: AgentId, agent_name: str) -> DiscoveredAgent:
    return DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName(agent_name),
        provider_name=ProviderInstanceName("docker"),
    )


def test_get_system_services_agent_id_finds_agent_sharing_the_host() -> None:
    """The system-services agent on the workspace agent's host is resolved, not one on another host."""
    resolver = MngrCliBackendResolver()
    host_a = HostId.generate()
    host_b = HostId.generate()
    workspace_agent = AgentId.generate()
    services_on_a = AgentId.generate()
    services_on_b = AgentId.generate()
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(workspace_agent, services_on_a, services_on_b),
            discovered_agents=(
                _discovered_agent(host_a, workspace_agent, "my-claude-agent"),
                _discovered_agent(host_a, services_on_a, "system-services"),
                _discovered_agent(host_b, services_on_b, "system-services"),
            ),
        )
    )

    assert resolver.get_system_services_agent_id(workspace_agent) == services_on_a


def test_get_system_services_agent_id_returns_none_when_not_discovered() -> None:
    resolver = MngrCliBackendResolver()
    assert resolver.get_system_services_agent_id(AgentId.generate()) is None


def _workspace_agent(
    host_id: HostId,
    agent_id: AgentId,
    extra_labels: Mapping[str, str] = {},
) -> DiscoveredAgent:
    """A primary-workspace DiscoveredAgent (carries the labels the workspace listing filters on)."""
    labels = {"workspace": "true", "is_primary": "true", **extra_labels}
    return DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName(str(agent_id)),
        provider_name=ProviderInstanceName("docker"),
        certified_data={"labels": labels},
    )


# -- get_workspace_color tests ----------------------------------------
#
# The color label is the storage substrate the workspace color picker
# writes to and the SSE workspaces payload reads from. Tests cover all
# four states the resolver may see on a label read:
#   1. label missing -> None (caller backfills)
#   2. label set, well-formed -> normalized lowercase hex
#   3. label set, lenient form -> normalized to canonical lowercase
#   4. label set, malformed -> DEFAULT_WORKSPACE_COLOR plus a single
#      once-per-agent warning log


def _resolver_with_workspace_agent(extra_labels: Mapping[str, str] = {}) -> tuple[MngrCliBackendResolver, AgentId]:
    """A resolver holding a single primary-workspace agent, returned with its id."""
    resolver = MngrCliBackendResolver()
    agent = AgentId.generate()
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(agent,),
            discovered_agents=(_workspace_agent(HostId.generate(), agent, extra_labels=extra_labels),),
        )
    )
    return resolver, agent


def test_get_workspace_color_returns_none_when_label_missing() -> None:
    """Pre-migration / freshly-created workspaces have no color label."""
    resolver, agent = _resolver_with_workspace_agent()
    assert resolver.get_workspace_color(agent) is None


def test_get_workspace_color_returns_normalized_hex_when_label_set() -> None:
    resolver, agent = _resolver_with_workspace_agent(extra_labels={"color": "#0b292b"})
    assert resolver.get_workspace_color(agent) == "#0b292b"


@pytest.mark.parametrize(
    ("stored_label", "expected_hex"),
    [
        ("#FFFFFF", "#ffffff"),
        ("ffffff", "#ffffff"),
        ("#fff", "#ffffff"),
        ("FFF", "#ffffff"),
        ("  #0b292b  ", "#0b292b"),
    ],
)
def test_get_workspace_color_normalizes_lenient_label_values(stored_label: str, expected_hex: str) -> None:
    resolver, agent = _resolver_with_workspace_agent(extra_labels={"color": stored_label})
    assert resolver.get_workspace_color(agent) == expected_hex


def test_get_workspace_color_recovers_to_default_when_label_malformed() -> None:
    """Mngr does not validate label values; a hand-edited / future-version
    label might be junk. The resolver returns the default workspace color
    rather than crashing the SSE generator."""
    resolver, agent = _resolver_with_workspace_agent(extra_labels={"color": "not-a-hex"})
    assert resolver.get_workspace_color(agent) == DEFAULT_WORKSPACE_COLOR


def test_get_workspace_color_returns_none_for_unknown_agent() -> None:
    resolver = MngrCliBackendResolver()
    assert resolver.get_workspace_color(AgentId.generate()) is None


def test_set_workspace_color_locally_updates_the_cached_label() -> None:
    """Optimistic write: after a successful CLI mngr label write, the
    resolver's cached snapshot is updated in place so the next SSE
    workspaces emit reflects the new color -- without waiting for the
    ~10s discovery tick to re-emit through ``mngr observe``."""
    resolver, agent = _resolver_with_workspace_agent()
    assert resolver.get_workspace_color(agent) is None

    assert resolver.set_workspace_color_locally(agent, "#0b292b") is True
    assert resolver.get_workspace_color(agent) == "#0b292b"


def test_set_workspace_color_locally_clears_the_malformed_log_marker() -> None:
    """After a successful write, the previously-logged-as-malformed marker
    is cleared so a future round-trip with a *different* bad value will
    log again. (The fix path -- repick from settings -- should leave the
    log in a useful state for any next failure.)"""
    resolver, agent = _resolver_with_workspace_agent(extra_labels={"color": "junk"})
    # First read triggers the warning + marks the agent as logged.
    resolver.get_workspace_color(agent)
    assert str(agent) in resolver._logged_malformed_color_agents

    resolver.set_workspace_color_locally(agent, "#ffffff")
    assert str(agent) not in resolver._logged_malformed_color_agents


def test_set_workspace_color_locally_returns_false_for_unknown_agent() -> None:
    resolver = MngrCliBackendResolver()
    assert resolver.set_workspace_color_locally(AgentId.generate(), "#0b292b") is False


def test_set_workspace_color_locally_fires_on_change_callbacks() -> None:
    """SSE subscribers register an on-change callback to wake on resolver
    mutations; the optimistic color write must fire it so the chrome
    repaints within one SSE tick of the settings save."""
    resolver, agent = _resolver_with_workspace_agent()

    callback_calls: list[int] = []
    resolver.add_on_change_callback(lambda: callback_calls.append(1))
    resolver.set_workspace_color_locally(agent, "#0b292b")
    assert callback_calls == [1]


def test_get_workspace_color_logs_each_malformed_agent_only_once() -> None:
    """Reads happen on every SSE tick; a malformed label must not spam
    the log. Subsequent reads for the same agent stay silent. Uses a
    loguru sink instead of caplog because loguru does not propagate
    to the standard logging module that caplog hooks."""
    resolver, agent = _resolver_with_workspace_agent(extra_labels={"color": "junk"})
    log_records: list[str] = []
    sink_id = loguru_logger.add(lambda msg: log_records.append(str(msg)), level="WARNING")
    try:
        resolver.get_workspace_color(agent)
        resolver.get_workspace_color(agent)
        resolver.get_workspace_color(agent)
    finally:
        loguru_logger.remove(sink_id)
    matching = [r for r in log_records if "malformed color" in r.lower()]
    assert len(matching) == 1, matching


def test_list_active_workspace_ids_excludes_agents_on_destroyed_hosts() -> None:
    """A workspace on a DESTROYED host stays in the known set but drops from the active set."""
    resolver = MngrCliBackendResolver()
    live_host = HostId.generate()
    dead_host = HostId.generate()
    live_agent = AgentId.generate()
    dead_agent = AgentId.generate()
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(live_agent, dead_agent),
            discovered_agents=(
                _workspace_agent(live_host, live_agent),
                _workspace_agent(dead_host, dead_agent),
            ),
            host_state_by_host_id={
                str(live_host): HostState.RUNNING,
                str(dead_host): HostState.DESTROYED,
            },
        )
    )

    assert set(resolver.list_known_workspace_ids()) == {live_agent, dead_agent}
    assert resolver.list_active_workspace_ids() == (live_agent,)


def test_list_active_workspace_ids_keeps_agents_whose_host_state_is_unknown() -> None:
    """Absent host state must not hide a workspace (only an explicit DESTROYED does)."""
    resolver = MngrCliBackendResolver()
    host = HostId.generate()
    agent = AgentId.generate()
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(agent,),
            discovered_agents=(_workspace_agent(host, agent),),
        )
    )

    assert resolver.list_active_workspace_ids() == (agent,)


def test_get_host_state_returns_known_state_and_none_otherwise() -> None:
    resolver = MngrCliBackendResolver()
    host = HostId.generate()
    agent = AgentId.generate()
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(agent,),
            discovered_agents=(_workspace_agent(host, agent),),
            host_state_by_host_id={str(host): HostState.DESTROYED},
        )
    )

    assert resolver.get_host_state(host) is HostState.DESTROYED
    assert resolver.get_host_state(HostId.generate()) is None


def _resolver_with_host_state(host: HostId, agent: AgentId, state: HostState | None) -> MngrCliBackendResolver:
    """A resolver carrying one workspace on ``host`` with the given discovery host state."""
    resolver = MngrCliBackendResolver()
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(agent,),
            discovered_agents=(_workspace_agent(host, agent),),
            host_state_by_host_id={str(host): state} if state is not None else {},
        )
    )
    return resolver


def test_host_state_override_wins_over_discovery_then_drops_on_agreement() -> None:
    """An optimistic override masks discovery until the next snapshot agrees, then is dropped."""
    host = HostId.generate()
    agent = AgentId.generate()
    resolver = _resolver_with_host_state(host, agent, HostState.RUNNING)

    # Discovery still says RUNNING, but the user just stopped it.
    resolver.set_host_state_override(host, HostState.STOPPED)
    assert resolver.get_host_state(host) is HostState.STOPPED

    # A fresh discovery snapshot that agrees (STOPPED) confirms and drops the override.
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(agent,),
            discovered_agents=(_workspace_agent(host, agent),),
            host_state_by_host_id={str(host): HostState.STOPPED},
        )
    )
    assert resolver.get_host_state(host) is HostState.STOPPED
    # Override is gone: a later discovery-only flip back to RUNNING is reflected, unmasked.
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(agent,),
            discovered_agents=(_workspace_agent(host, agent),),
            host_state_by_host_id={str(host): HostState.RUNNING},
        )
    )
    assert resolver.get_host_state(host) is HostState.RUNNING


def test_clear_host_state_override_reverts_to_discovery() -> None:
    host = HostId.generate()
    agent = AgentId.generate()
    resolver = _resolver_with_host_state(host, agent, HostState.RUNNING)
    resolver.set_host_state_override(host, HostState.STOPPED)

    resolver.clear_host_state_override(host)

    assert resolver.get_host_state(host) is HostState.RUNNING


def test_host_state_override_fires_on_change_on_set_and_clear() -> None:
    host = HostId.generate()
    resolver = MngrCliBackendResolver()
    changes: list[None] = []
    resolver.add_on_change_callback(lambda: changes.append(None))

    resolver.set_host_state_override(host, HostState.STOPPED)
    # Clearing an absent override is a no-op (no extra fire); clearing a present one fires.
    resolver.clear_host_state_override(HostId.generate())
    resolver.clear_host_state_override(host)

    assert len(changes) == 2


def test_host_state_override_does_not_affect_active_workspace_filtering() -> None:
    """The override only ever holds RUNNING/STOPPED, so it can't change the DESTROYED-only filter."""
    host = HostId.generate()
    agent = AgentId.generate()
    resolver = _resolver_with_host_state(host, agent, HostState.RUNNING)

    resolver.set_host_state_override(host, HostState.STOPPED)

    # A stopped (overridden) host is still an active workspace -- only DESTROYED drops it.
    assert resolver.list_active_workspace_ids() == (agent,)


def test_parse_agents_from_json_extracts_host_state() -> None:
    """mngr list --format json carries host.state, which parsing surfaces per host id."""
    json_output = json.dumps(
        {
            "agents": [
                {"id": str(_AGENT_A), "host": {"id": "host-aaaa", "state": "RUNNING"}},
                {"id": str(_AGENT_B), "host": {"id": "host-bbbb", "state": "DESTROYED"}},
            ]
        }
    )
    result = parse_agents_from_json(json_output)
    assert result.host_state_by_host_id == {
        "host-aaaa": HostState.RUNNING,
        "host-bbbb": HostState.DESTROYED,
    }


def _pair_snapshot(
    host: HostId, workspace_agent: AgentId, services_agent: AgentId
) -> tuple[DiscoveredAgent, DiscoveredAgent]:
    """A complete two-agent host snapshot: a claude workspace agent + its system-services agent."""
    return (
        _discovered_agent(host, workspace_agent, "my-claude-agent"),
        _discovered_agent(host, services_agent, "system-services"),
    )


def test_last_good_topology_persists_and_reloads_across_restart(tmp_path: Path) -> None:
    """A complete host enumeration is written to disk and resolves from a fresh resolver.

    Covers the cross-minds-restart case: the in-memory topology starts empty
    on a new process, so the persisted file is the only source.
    """
    topology_path = tmp_path / "last_good_agent_topology.json"
    host = HostId.generate()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()

    resolver = MngrCliBackendResolver(last_good_agents_path=topology_path)
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(workspace_agent, services_agent),
            discovered_agents=_pair_snapshot(host, workspace_agent, services_agent),
        )
    )
    assert topology_path.exists()

    reloaded = MngrCliBackendResolver(last_good_agents_path=topology_path)
    assert reloaded.get_system_services_agent_id(workspace_agent) == services_agent


def test_last_good_topology_falls_back_when_discovery_loses_the_host(tmp_path: Path) -> None:
    """After a complete enumeration, discovery losing the whole host still resolves via last-good.

    Reproduces the SSH-dead failure mode: the docker provider's enumeration
    falls over and ``update_agents`` is called with an empty agent list. The
    restart path must still be able to address the system-services agent so
    a host restart can proceed.
    """
    topology_path = tmp_path / "last_good_agent_topology.json"
    host = HostId.generate()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = MngrCliBackendResolver(last_good_agents_path=topology_path)
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(workspace_agent, services_agent),
            discovered_agents=_pair_snapshot(host, workspace_agent, services_agent),
        )
    )
    # SSH dies; discovery loses every agent on the host.
    resolver.update_agents(ParsedAgentsResult())

    assert resolver.get_system_services_agent_id(workspace_agent) == services_agent


def test_last_good_topology_preserves_other_host_on_partial_discovery_loss() -> None:
    """A snapshot missing one host must not erase that host's remembered pairing.

    Two workspaces on two hosts are both seen; then a later snapshot enumerates
    only host A (host B's SSH died). Host B's pairing must survive in last-good
    even though A produced a fresh, non-empty snapshot -- otherwise the very
    workspace a user is trying to restart loses its services agent the moment
    any sibling workspace reports in.
    """
    resolver = MngrCliBackendResolver()
    host_a = HostId.generate()
    host_b = HostId.generate()
    workspace_a, services_a = AgentId.generate(), AgentId.generate()
    workspace_b, services_b = AgentId.generate(), AgentId.generate()
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(workspace_a, services_a, workspace_b, services_b),
            discovered_agents=(
                *_pair_snapshot(host_a, workspace_a, services_a),
                *_pair_snapshot(host_b, workspace_b, services_b),
            ),
        )
    )
    # Host B drops out; only host A is enumerated this round.
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(workspace_a, services_a),
            discovered_agents=_pair_snapshot(host_a, workspace_a, services_a),
        )
    )

    # Host A resolves from the live snapshot; host B resolves from the last-good fallback.
    assert resolver.get_system_services_agent_id(workspace_a) == services_a
    assert resolver.get_system_services_agent_id(workspace_b) == services_b


def test_last_good_topology_ignores_incomplete_host_enumeration() -> None:
    """A host snapshot lacking the system-services agent must not clobber the remembered pairing.

    Models a within-host partial discovery loss: the workspace agent is still
    visible but its system-services agent dropped. Since the enumeration is
    incomplete (no system-services agent on the host), last-good keeps the
    prior complete record and the fallback still resolves the services agent.
    """
    resolver = MngrCliBackendResolver()
    host = HostId.generate()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(workspace_agent, services_agent),
            discovered_agents=_pair_snapshot(host, workspace_agent, services_agent),
        )
    )
    # Only the workspace agent remains visible; the system-services agent dropped.
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(workspace_agent,),
            discovered_agents=(_discovered_agent(host, workspace_agent, "my-claude-agent"),),
        )
    )

    assert resolver.get_system_services_agent_id(workspace_agent) == services_agent


def test_last_good_topology_ignores_malformed_persisted_file(tmp_path: Path) -> None:
    """A garbage topology file is treated as empty rather than crashing minds startup."""
    topology_path = tmp_path / "last_good_agent_topology.json"
    topology_path.write_text("not json {{{", encoding="utf-8")

    resolver = MngrCliBackendResolver(last_good_agents_path=topology_path)
    assert resolver.get_system_services_agent_id(AgentId.generate()) is None


def test_last_good_topology_prefers_live_discovery_when_host_present(tmp_path: Path) -> None:
    """Last-good is a fallback, not an override: live discovery wins when the host is visible."""
    topology_path = tmp_path / "last_good_agent_topology.json"
    host = HostId.generate()
    workspace_agent = AgentId.generate()
    stale_services_agent = AgentId.generate()
    current_services_agent = AgentId.generate()

    # Seed the persisted topology with a now-stale services agent via a first resolver.
    seed = MngrCliBackendResolver(last_good_agents_path=topology_path)
    seed.update_agents(
        ParsedAgentsResult(
            agent_ids=(workspace_agent, stale_services_agent),
            discovered_agents=_pair_snapshot(host, workspace_agent, stale_services_agent),
        )
    )

    resolver = MngrCliBackendResolver(last_good_agents_path=topology_path)
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(workspace_agent, current_services_agent),
            discovered_agents=_pair_snapshot(host, workspace_agent, current_services_agent),
        )
    )

    assert resolver.get_system_services_agent_id(workspace_agent) == current_services_agent


def test_mngr_cli_resolver_update_services_replaces_state() -> None:
    """Calling update_services replaces the service map for that agent."""
    resolver = MngrCliBackendResolver()

    resolver.update_services(_AGENT_A, {"web": "http://127.0.0.1:9100"})
    assert resolver.get_backend_url(_AGENT_A, _SERVICE_WEB) == "http://127.0.0.1:9100"

    resolver.update_services(_AGENT_A, {"web": "http://127.0.0.1:9200"})
    assert resolver.get_backend_url(_AGENT_A, _SERVICE_WEB) == "http://127.0.0.1:9200"


# -- parse_agents_from_json tests --


def _make_agents_json_with_ssh(*agents: tuple[str, Mapping[str, object] | None]) -> str:
    """Build mngr list --format json output with optional SSH info per agent."""
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
        "key_path": "/home/user/.mngr/providers/modal/modal_ssh_key",
    }
    json_str = _make_agents_json_with_ssh((str(_AGENT_A), ssh_data))
    result = parse_agents_from_json(json_str)

    ssh_info = result.ssh_info_by_agent_id.get(str(_AGENT_A))
    assert ssh_info is not None
    assert ssh_info.user == "root"
    assert ssh_info.host == "remote.example.com"
    assert ssh_info.port == 12345
    assert ssh_info.key_path == Path("/home/user/.mngr/providers/modal/modal_ssh_key")


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


def test_parse_agents_from_json_skips_entries_missing_id() -> None:
    """Agents without an 'id' field in the output are skipped."""
    json_str = json.dumps({"agents": [{"name": "no-id-agent"}]})
    result = parse_agents_from_json(json_str)
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


# -- MngrCliBackendResolver.get_ssh_info tests --


def test_mngr_cli_resolver_get_ssh_info_returns_info_for_remote_agent() -> None:
    ssh_data = {
        "user": "root",
        "host": "remote.example.com",
        "port": 12345,
        "key_path": "/tmp/test_key",
    }
    agents_json = _make_agents_json_with_ssh((str(_AGENT_A), ssh_data))
    resolver = make_resolver_with_data(service_logs={}, agents_json=agents_json)

    ssh_info = resolver.get_ssh_info(_AGENT_A)
    assert ssh_info is not None
    assert ssh_info.host == "remote.example.com"
    assert ssh_info.port == 12345


def test_mngr_cli_resolver_get_ssh_info_returns_none_for_local_agent() -> None:
    agents_json = _make_agents_json_with_ssh((str(_AGENT_A), None))
    resolver = make_resolver_with_data(service_logs={}, agents_json=agents_json)

    assert resolver.get_ssh_info(_AGENT_A) is None


def test_mngr_cli_resolver_get_ssh_info_returns_none_for_unknown_agent() -> None:
    agents_json = _make_agents_json_with_ssh((str(_AGENT_A), None))
    resolver = make_resolver_with_data(service_logs={}, agents_json=agents_json)

    assert resolver.get_ssh_info(_AGENT_B) is None


# -- BackendResolverInterface.get_ssh_info default --


def test_backend_resolver_interface_default_get_ssh_info_returns_none() -> None:
    """The base class default get_ssh_info returns None for all agents."""

    class MinimalResolver(BackendResolverInterface):
        def get_backend_url(self, agent_id: AgentId, service_name: ServiceName) -> str | None:
            return None

        def list_known_agent_ids(self) -> tuple[AgentId, ...]:
            return ()

        def list_services_for_agent(self, agent_id: AgentId) -> tuple[ServiceName, ...]:
            return ()

    resolver = MinimalResolver()
    assert resolver.get_ssh_info(_AGENT_A) is None


# -- MngrCliBackendResolver.get_agent_display_info tests --


def test_mngr_cli_resolver_get_agent_display_info_returns_info_for_known_agent() -> None:
    agents_json = make_agents_json(_AGENT_A)
    resolver = make_resolver_with_data(agents_json=agents_json, service_logs={})

    info = resolver.get_agent_display_info(_AGENT_A)
    assert info is not None
    assert isinstance(info, AgentDisplayInfo)
    assert info.agent_name == str(_AGENT_A)


def test_mngr_cli_resolver_get_agent_display_info_returns_none_for_unknown_agent() -> None:
    agents_json = make_agents_json(_AGENT_A)
    resolver = make_resolver_with_data(agents_json=agents_json, service_logs={})

    assert resolver.get_agent_display_info(_AGENT_B) is None


# -- BackendResolverInterface.get_agent_display_info default --


def test_backend_resolver_interface_default_get_agent_display_info_returns_info_for_known() -> None:
    """The base class default get_agent_display_info returns info using agent_id as name."""

    class MinimalResolver(BackendResolverInterface):
        def get_backend_url(self, agent_id: AgentId, service_name: ServiceName) -> str | None:
            return None

        def list_known_agent_ids(self) -> tuple[AgentId, ...]:
            return (_AGENT_A,)

        def list_services_for_agent(self, agent_id: AgentId) -> tuple[ServiceName, ...]:
            return ()

    resolver = MinimalResolver()
    info = resolver.get_agent_display_info(_AGENT_A)
    assert info is not None
    assert info.agent_name == str(_AGENT_A)
    assert info.host_id == "localhost"


def test_backend_resolver_interface_default_get_agent_display_info_returns_none_for_unknown() -> None:
    """The base class default get_agent_display_info returns None for unknown agents."""

    class MinimalResolver(BackendResolverInterface):
        def get_backend_url(self, agent_id: AgentId, service_name: ServiceName) -> str | None:
            return None

        def list_known_agent_ids(self) -> tuple[AgentId, ...]:
            return ()

        def list_services_for_agent(self, agent_id: AgentId) -> tuple[ServiceName, ...]:
            return ()

    resolver = MinimalResolver()
    assert resolver.get_agent_display_info(_AGENT_A) is None
