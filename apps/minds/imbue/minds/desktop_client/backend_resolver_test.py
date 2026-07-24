import json
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
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
from imbue.minds.desktop_client.backend_resolver import WORKSPACE_DISPLAY_NAME_LABEL
from imbue.minds.desktop_client.backend_resolver import _read_last_good_agent_topology
from imbue.minds.desktop_client.backend_resolver import parse_agent_ids_from_json
from imbue.minds.desktop_client.backend_resolver import parse_agents_from_json
from imbue.minds.desktop_client.backend_resolver import parse_service_log_records
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_service_log
from imbue.minds.desktop_client.conftest import seed_provider_snapshots
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.primitives import ServiceName
from imbue.mngr.api.discovery_events import DiscoveryError
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


def _primary_system_services_agent(host_id: HostId, agent_id: AgentId) -> DiscoveredAgent:
    """A minds primary workspace agent: the system-services agent, which carries the workspace labels.

    In minds the user-facing workspace agent IS the host's system-services
    agent -- it has both the ``workspace`` + ``is_primary`` labels (the live
    filter) and the constant system-services name (the last-good filter).
    """
    return DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName("system-services"),
        provider_name=ProviderInstanceName("docker"),
        certified_data={"labels": {"workspace": "true", "is_primary": "true"}},
    )


def test_list_restorable_workspace_ids_keeps_workspace_after_discovery_loss(tmp_path: Path) -> None:
    """A workspace absent from the live snapshot is still restorable via the last-good topology.

    The cold-start race: a slow provider hasn't re-listed the workspace yet, so
    the live list is empty -- but the restore view must still recognize it so its
    window is not dropped.
    """
    topology_path = tmp_path / "last_good_agent_topology.json"
    host = HostId.generate()
    agent = AgentId.generate()
    resolver = MngrCliBackendResolver(last_good_agents_path=topology_path)
    resolver.update_agents(
        ParsedAgentsResult(agent_ids=(agent,), discovered_agents=(_primary_system_services_agent(host, agent),))
    )
    assert resolver.list_active_workspace_ids() == (agent,)
    assert resolver.list_restorable_workspace_ids() == (agent,)

    # Discovery loses the host (empty snapshot); live drops it, last-good keeps it.
    resolver.update_agents(ParsedAgentsResult())
    assert resolver.list_active_workspace_ids() == ()
    assert resolver.list_restorable_workspace_ids() == (agent,)


def test_list_restorable_workspace_ids_unions_live_and_last_good(tmp_path: Path) -> None:
    """The restorable set is the union of last-good and a freshly-discovered (not-yet-remembered) workspace."""
    topology_path = tmp_path / "last_good_agent_topology.json"
    remembered_host, remembered_agent = HostId.generate(), AgentId.generate()
    fresh_host, fresh_agent = HostId.generate(), AgentId.generate()
    resolver = MngrCliBackendResolver(last_good_agents_path=topology_path)
    # A complete enumeration of the remembered workspace lands in last-good.
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(remembered_agent,),
            discovered_agents=(_primary_system_services_agent(remembered_host, remembered_agent),),
        )
    )
    # A later snapshot lists only a different, fresh workspace (remembered one absent live).
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(fresh_agent,),
            discovered_agents=(_primary_system_services_agent(fresh_host, fresh_agent),),
        )
    )
    assert set(resolver.list_restorable_workspace_ids()) == {remembered_agent, fresh_agent}


def test_last_good_topology_prunes_host_on_observed_destroyed(tmp_path: Path) -> None:
    """A host observed DESTROYED is pruned from last-good, and the prune is persisted to disk.

    Reproduces the user destroying their workspace: a complete enumeration lands
    the host in last-good, then a later snapshot reports it DESTROYED (its agents
    linger in discovery for the provider's persistence window). The host must be
    dropped from both the in-memory fallback and the on-disk topology so it stops
    counting as restorable.
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
    assert resolver.get_system_services_agent_id(workspace_agent) == services_agent
    assert str(host) in _read_last_good_agent_topology(topology_path).agents_by_host

    # The host is later observed DESTROYED (its agents still linger in discovery).
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(workspace_agent, services_agent),
            discovered_agents=_pair_snapshot(host, workspace_agent, services_agent),
            host_state_by_host_id={str(host): HostState.DESTROYED},
        )
    )
    # The host is dropped from the persisted topology (the on-disk write path).
    assert str(host) not in _read_last_good_agent_topology(topology_path).agents_by_host

    # Once discovery finally drops the lingering agents, the fallback has nothing
    # to resolve -- proving the prune, not the (now-empty) live snapshot, is what
    # removed it. A merely-absent host would still resolve here; a DESTROYED one
    # does not.
    resolver.update_agents(ParsedAgentsResult())
    assert resolver.get_system_services_agent_id(workspace_agent) is None
    # A fresh resolver reloading from disk agrees: the prune survived the restart.
    reloaded = MngrCliBackendResolver(last_good_agents_path=topology_path)
    assert reloaded.get_system_services_agent_id(workspace_agent) is None


def _services_agent_for_provider(host_id: HostId, agent_id: AgentId, provider: str) -> DiscoveredAgent:
    """A system-services agent record attributed to ``provider``."""
    return DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName("system-services"),
        provider_name=ProviderInstanceName(provider),
    )


def test_last_good_topology_prunes_provider_host_absent_from_clean_snapshot(tmp_path: Path) -> None:
    """A clean provider snapshot is authoritative: its vanished hosts are pruned from last-good.

    Reproduces the ghost-workspace pathology: a cloud host is reaped server-side
    while this client is not watching, so no DESTROYED observation ever arrives
    and the DESTROYED-only prune keeps it "restorable" forever (stranding the
    landing page on the discovering state). A clean (error-free) snapshot for
    that provider that no longer lists the host is positive absence evidence and
    must prune it -- in memory and on disk -- while hosts the snapshot still
    reports and hosts of other providers are untouched.
    """
    topology_path = tmp_path / "last_good_agent_topology.json"
    cloud_host, cloud_agent = HostId.generate(), AgentId.generate()
    docker_host, docker_agent = HostId.generate(), AgentId.generate()
    resolver = MngrCliBackendResolver(last_good_agents_path=topology_path)
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(cloud_agent, docker_agent),
            discovered_agents=(
                _services_agent_for_provider(cloud_host, cloud_agent, "imbue_cloud_user"),
                _services_agent_for_provider(docker_host, docker_agent, "docker"),
            ),
        )
    )
    assert str(cloud_host) in _read_last_good_agent_topology(topology_path).agents_by_host

    # The cloud provider polls cleanly and no longer lists its host; docker's
    # clean poll still reports its own host.
    resolver.update_providers(
        provider_name=ProviderInstanceName("imbue_cloud_user"),
        provider=None,
        error=None,
        last_snapshot_at=datetime.now(timezone.utc),
        clean_snapshot_host_ids=(),
    )
    resolver.update_providers(
        provider_name=ProviderInstanceName("docker"),
        provider=None,
        error=None,
        last_snapshot_at=datetime.now(timezone.utc),
        clean_snapshot_host_ids=(str(docker_host),),
    )

    remembered = _read_last_good_agent_topology(topology_path).agents_by_host
    assert str(cloud_host) not in remembered
    assert str(docker_host) in remembered
    assert set(resolver.list_restorable_workspace_ids()) == {docker_agent}


def test_last_good_topology_retains_provider_hosts_on_errored_snapshot(tmp_path: Path) -> None:
    """An errored provider poll proves nothing: its remembered hosts are retained.

    The provider's hosts are unreachable, not absent, so the caller passes no
    host set and nothing may be pruned -- the entire point of the last-good
    fallback is surviving exactly this kind of discovery loss.
    """
    topology_path = tmp_path / "last_good_agent_topology.json"
    cloud_host, cloud_agent = HostId.generate(), AgentId.generate()
    provider = ProviderInstanceName("imbue_cloud_user")
    resolver = MngrCliBackendResolver(last_good_agents_path=topology_path)
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(cloud_agent,),
            discovered_agents=(_services_agent_for_provider(cloud_host, cloud_agent, str(provider)),),
        )
    )

    resolver.update_providers(
        provider_name=provider,
        provider=None,
        error=DiscoveryError(type_name="ProviderUnavailableError", message="cloud down", provider_name=provider),
        last_snapshot_at=datetime.now(timezone.utc),
        clean_snapshot_host_ids=None,
    )

    assert str(cloud_host) in _read_last_good_agent_topology(topology_path).agents_by_host
    assert set(resolver.list_restorable_workspace_ids()) == {cloud_agent}


def test_last_good_topology_resets_legacy_file_without_provider_names(tmp_path: Path) -> None:
    """A pre-provider-attribution topology file fails validation and loads as empty.

    Deliberate migration: unattributed records can never be pruned by a clean
    provider snapshot, so accumulated ghosts (hosts long gone without a
    DESTROYED observation) would strand the landing page on the discovering
    state forever. Live workspaces re-enter the topology on their next
    discovery snapshot; only the stale memories are lost.
    """
    topology_path = tmp_path / "last_good_agent_topology.json"
    host, agent = HostId.generate(), AgentId.generate()
    topology_path.write_text(
        json.dumps(
            {
                "agents_by_host": {
                    str(host): [{"agent_id": str(agent), "host_id": str(host), "agent_name": "system-services"}]
                }
            }
        ),
        encoding="utf-8",
    )

    resolver = MngrCliBackendResolver(last_good_agents_path=topology_path)

    assert resolver.list_restorable_workspace_ids() == ()
    assert resolver.get_system_services_agent_id(agent) is None


def test_last_good_topology_keeps_absent_host_but_prunes_destroyed_one() -> None:
    """A host merely absent from a later snapshot is retained; only a DESTROYED one is pruned.

    Guards the core last-good behavior against C1's prune: dropping on absence
    would defeat the slow-cold-start fallback. Here one host vanishes from the
    snapshot entirely while a sibling is observed DESTROYED in the same tick --
    the absent host must survive (absence is not destruction) and only the
    DESTROYED host is pruned.
    """
    resolver = MngrCliBackendResolver()
    absent_host, absent_ws, absent_svc = HostId.generate(), AgentId.generate(), AgentId.generate()
    dead_host, dead_ws, dead_svc = HostId.generate(), AgentId.generate(), AgentId.generate()
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(absent_ws, absent_svc, dead_ws, dead_svc),
            discovered_agents=(
                *_pair_snapshot(absent_host, absent_ws, absent_svc),
                *_pair_snapshot(dead_host, dead_ws, dead_svc),
            ),
        )
    )
    # A later snapshot omits the first host entirely and reports the second
    # DESTROYED (its agents still linger in discovery for the persistence window).
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(dead_ws, dead_svc),
            discovered_agents=_pair_snapshot(dead_host, dead_ws, dead_svc),
            host_state_by_host_id={str(dead_host): HostState.DESTROYED},
        )
    )
    # A final snapshot drops the lingering DESTROYED agents too, leaving the
    # fallback as the only source for both hosts -- so what survives is exactly
    # what the prune kept.
    resolver.update_agents(ParsedAgentsResult())

    # Absent host is retained (fallback still resolves); DESTROYED host is pruned.
    assert resolver.get_system_services_agent_id(absent_ws) == absent_svc
    assert resolver.get_system_services_agent_id(dead_ws) is None


def test_list_restorable_workspace_ids_excludes_destroyed_host_in_live_snapshot(tmp_path: Path) -> None:
    """A live workspace whose host is DESTROYED is excluded from the restorable set.

    The destroyed host lingers in discovery for the provider's persistence
    window, but its window must not be restored: "restorable" means genuinely
    not-destroyed, mirroring ``list_active_workspace_ids``.
    """
    topology_path = tmp_path / "last_good_agent_topology.json"
    live_host, live_agent = HostId.generate(), AgentId.generate()
    dead_host, dead_agent = HostId.generate(), AgentId.generate()
    resolver = MngrCliBackendResolver(last_good_agents_path=topology_path)
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(live_agent, dead_agent),
            discovered_agents=(
                _primary_system_services_agent(live_host, live_agent),
                _primary_system_services_agent(dead_host, dead_agent),
            ),
            host_state_by_host_id={
                str(live_host): HostState.RUNNING,
                str(dead_host): HostState.DESTROYED,
            },
        )
    )

    assert resolver.list_restorable_workspace_ids() == (live_agent,)


def test_list_restorable_workspace_ids_empties_after_sole_workspace_destroyed(tmp_path: Path) -> None:
    """Destroying the only workspace empties the restorable set (the spinner-vs-create-form bug).

    Covers the union's last-good half: after a complete enumeration lands the
    workspace in last-good, observing its host DESTROYED must both prune last-good
    (C1) and exclude the still-lingering live entry (C2), so restorable is empty --
    and stays empty once discovery finally drops the lingering agent. Otherwise the
    landing handler keeps the "Discovering..." spinner up instead of the create form.
    """
    topology_path = tmp_path / "last_good_agent_topology.json"
    host, agent = HostId.generate(), AgentId.generate()
    resolver = MngrCliBackendResolver(last_good_agents_path=topology_path)
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(agent,),
            discovered_agents=(_primary_system_services_agent(host, agent),),
        )
    )
    assert resolver.list_restorable_workspace_ids() == (agent,)

    # The host is observed DESTROYED while its agent still lingers in discovery.
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(agent,),
            discovered_agents=(_primary_system_services_agent(host, agent),),
            host_state_by_host_id={str(host): HostState.DESTROYED},
        )
    )
    assert resolver.list_restorable_workspace_ids() == ()

    # Discovery finally drops the lingering agent; last-good stays pruned (not resurrected).
    resolver.update_agents(ParsedAgentsResult())
    assert resolver.list_restorable_workspace_ids() == ()


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


# -- is_agent_provider_errored tests --


def _local_provider_error() -> DiscoveryError:
    return DiscoveryError(
        type_name="RuntimeError",
        message="limactl crashed",
        provider_name=ProviderInstanceName("local"),
    )


def test_is_agent_provider_errored_true_when_agents_provider_errored() -> None:
    """A discovered agent whose provider has a surfaced discovery error reports True."""
    resolver = make_resolver_with_data(agents_json=make_agents_json(_AGENT_A))
    seed_provider_snapshots(resolver, error_by_provider_name={ProviderInstanceName("local"): _local_provider_error()})

    assert resolver.is_agent_provider_errored(_AGENT_A) is True


def test_is_agent_provider_errored_false_when_provider_healthy() -> None:
    """A discovered agent whose provider has no surfaced error reports False."""
    resolver = make_resolver_with_data(agents_json=make_agents_json(_AGENT_A))

    assert resolver.is_agent_provider_errored(_AGENT_A) is False


def test_is_agent_provider_errored_false_for_unknown_agent() -> None:
    """An agent absent from the live snapshot has no attributable provider, so False.

    This is the cold-start edge: even with the provider errored, an agent that
    discovery has not enumerated cannot be tied to that provider.
    """
    resolver = make_resolver_with_data(agents_json=make_agents_json(_AGENT_A))
    seed_provider_snapshots(resolver, error_by_provider_name={ProviderInstanceName("local"): _local_provider_error()})

    assert resolver.is_agent_provider_errored(_AGENT_B) is False


def test_is_agent_provider_errored_default_false_for_static_resolver() -> None:
    """Resolvers without provider state (the interface default) never report an error."""
    resolver = StaticBackendResolver(url_by_agent_and_service={str(_AGENT_A): {"web": "http://127.0.0.1:9100"}})

    assert resolver.is_agent_provider_errored(_AGENT_A) is False


# -- workspace name override (optimistic rename) tests ----------------
#
# A UI rename writes the new name via ``mngr label`` / ``mngr rename``, but
# the settings page reads the name from the discovery-fed cache, which lags.
# ``set_workspace_name_override`` masks that lag until discovery re-reads the
# renamed labels; these tests cover a display-only rename and a slug rename.


def test_workspace_name_override_masks_stale_discovery_then_is_swept() -> None:
    """A display-only rename override wins over the stale label, then is dropped once discovery agrees."""
    resolver = MngrCliBackendResolver()
    host = HostId.generate()
    agent = AgentId.generate()

    def _snapshot(display_name: str) -> ParsedAgentsResult:
        return ParsedAgentsResult(
            agent_ids=(agent,),
            discovered_agents=(
                _workspace_agent(host, agent, extra_labels={WORKSPACE_DISPLAY_NAME_LABEL: display_name}),
            ),
        )

    resolver.update_agents(_snapshot("old-name"))
    assert resolver.get_workspace_name(agent) == "old-name"

    # The optimistic override wins immediately, before discovery re-reads the label.
    resolver.set_workspace_name_override(agent, "New Name", None)
    assert resolver.get_workspace_name(agent) == "New Name"

    # A still-stale snapshot (label not yet updated) does not clobber the override.
    resolver.update_agents(_snapshot("old-name"))
    assert resolver.get_workspace_name(agent) == "New Name"

    # Once discovery reports the new name, the override is swept: a later snapshot
    # with a different name is reflected directly (proving no override lingers).
    resolver.update_agents(_snapshot("New Name"))
    resolver.update_agents(_snapshot("Another Name"))
    assert resolver.get_workspace_name(agent) == "Another Name"


def test_workspace_name_override_covers_host_name_on_slug_rename() -> None:
    """A slug-changing rename optimistically overrides both the display name and the host name."""
    resolver = MngrCliBackendResolver()
    host = HostId.generate()
    agent = AgentId.generate()

    def _snapshot(display_name: str, slug: str) -> ParsedAgentsResult:
        return ParsedAgentsResult(
            agent_ids=(agent,),
            discovered_agents=(
                _workspace_agent(host, agent, extra_labels={WORKSPACE_DISPLAY_NAME_LABEL: display_name}),
            ),
            host_name_by_host_id={str(host): slug},
        )

    resolver.update_agents(_snapshot("Old Name", "old-slug"))
    assert resolver.get_workspace_name(agent) == "Old Name"
    assert resolver.get_host_name(agent) == "old-slug"

    resolver.set_workspace_name_override(agent, "New Name", "new-slug")
    assert resolver.get_workspace_name(agent) == "New Name"
    assert resolver.get_host_name(agent) == "new-slug"

    # A stale snapshot clobbers neither field.
    resolver.update_agents(_snapshot("Old Name", "old-slug"))
    assert resolver.get_host_name(agent) == "new-slug"

    # Discovery agreeing on both fields sweeps the override; a later change flows through.
    resolver.update_agents(_snapshot("New Name", "new-slug"))
    resolver.update_agents(_snapshot("Other Name", "other-slug"))
    assert resolver.get_workspace_name(agent) == "Other Name"
    assert resolver.get_host_name(agent) == "other-slug"
