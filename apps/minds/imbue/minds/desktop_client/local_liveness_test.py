import json
from datetime import datetime
from datetime import timezone

from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.local_liveness import LocalMindLivenessTracker
from imbue.minds.desktop_client.local_liveness import LocalMindState
from imbue.minds.desktop_client.local_liveness import build_local_host_state_list_argv
from imbue.minds.desktop_client.local_liveness import get_local_provider_names
from imbue.minds.desktop_client.local_liveness import get_local_workspace_agent_ids
from imbue.minds.desktop_client.local_liveness import parse_local_mind_states_from_list_json
from imbue.mngr.api.discovery_events import DiscoveredProvider
from imbue.mngr.api.discovery_events import make_discovered_provider
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName

_HOST = HostId("host-" + "0" * 31 + "1")


def _provider(name: str, backend: str) -> DiscoveredProvider:
    return make_discovered_provider(
        ProviderInstanceName(name),
        ProviderInstanceConfig(backend=ProviderBackendName(backend), is_enabled=True),
    )


def _workspace_agent(agent_id: AgentId, provider_name: str, host: HostId = _HOST) -> DiscoveredAgent:
    return DiscoveredAgent(
        host_id=host,
        agent_id=agent_id,
        agent_name=AgentName("ws-agent"),
        provider_name=ProviderInstanceName(provider_name),
        certified_data={"labels": {"workspace": "my-workspace", "is_primary": "true"}},
    )


def _list_json(agent_states: dict[str, str]) -> str:
    return json.dumps({"agents": [{"id": aid, "host": {"state": state}} for aid, state in agent_states.items()]})


# -- host-state parsing / classification --


def test_parse_local_mind_states_classifies_running_stopped_unknown() -> None:
    running = AgentId.generate()
    stopped = AgentId.generate()
    crashed = AgentId.generate()
    weird = AgentId.generate()
    list_json = _list_json(
        {
            str(running): "RUNNING",
            str(stopped): "STOPPED",
            str(crashed): "CRASHED",
            str(weird): "PAUSED",
        }
    )

    states = parse_local_mind_states_from_list_json(list_json, (running, stopped, crashed, weird))

    assert states[str(running)] == LocalMindState.RUNNING
    assert states[str(stopped)] == LocalMindState.STOPPED
    # CRASHED counts as offline -> STOPPED; an unrecognized state -> UNKNOWN.
    assert states[str(crashed)] == LocalMindState.STOPPED
    assert states[str(weird)] == LocalMindState.UNKNOWN


def test_parse_local_mind_states_marks_missing_agents_unknown() -> None:
    present = AgentId.generate()
    absent = AgentId.generate()
    list_json = _list_json({str(present): "RUNNING"})

    states = parse_local_mind_states_from_list_json(list_json, (present, absent))

    assert states[str(present)] == LocalMindState.RUNNING
    # An agent whose host could not be enumerated is unknown, not assumed stopped.
    assert states[str(absent)] == LocalMindState.UNKNOWN


def test_parse_local_mind_states_handles_malformed_json() -> None:
    agent = AgentId.generate()

    states = parse_local_mind_states_from_list_json("not valid json", (agent,))

    assert states[str(agent)] == LocalMindState.UNKNOWN


# -- argv building --


def test_build_local_host_state_list_argv_scopes_to_providers_and_agents() -> None:
    agent_a = AgentId.generate()
    agent_b = AgentId.generate()

    argv = build_local_host_state_list_argv("mngr", ("docker", "lima"), (agent_a, agent_b))

    assert argv[:2] == ["mngr", "list"]
    assert "json" in argv
    # Scopes discovery fan-out to local providers only (never touches remote ones).
    assert argv.count("--provider") == 2
    assert "docker" in argv and "lima" in argv
    # Includes only the workspaces we care about.
    include_value = argv[argv.index("--include") + 1]
    assert str(agent_a) in include_value
    assert str(agent_b) in include_value
    assert "--on-error" in argv


def test_build_local_host_state_list_argv_omits_include_without_agents() -> None:
    argv = build_local_host_state_list_argv("mngr", ("docker",), ())
    assert "--include" not in argv


# -- local classification from a resolver --


def test_get_local_workspace_agent_ids_keeps_only_local_backends() -> None:
    resolver = MngrCliBackendResolver()
    local_agent = AgentId.generate()
    remote_agent = AgentId.generate()
    resolver.update_providers(
        providers=(_provider("docker", "docker"), _provider("modal", "modal")),
        error_by_provider_name={},
        last_full_snapshot_at=datetime.now(timezone.utc),
    )
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(local_agent, remote_agent),
            discovered_agents=(
                _workspace_agent(local_agent, "docker"),
                _workspace_agent(remote_agent, "modal", host=HostId("host-" + "0" * 31 + "2")),
            ),
        )
    )

    local_ids = get_local_workspace_agent_ids(resolver)

    assert local_ids == (local_agent,)


def test_get_local_provider_names_returns_docker_and_lima_only() -> None:
    resolver = MngrCliBackendResolver()
    resolver.update_providers(
        providers=(_provider("docker", "docker"), _provider("my-lima", "lima"), _provider("modal", "modal")),
        error_by_provider_name={},
        last_full_snapshot_at=datetime.now(timezone.utc),
    )

    names = set(get_local_provider_names(resolver))

    assert names == {"docker", "my-lima"}


def test_get_local_workspace_agent_ids_empty_for_non_mngr_resolver() -> None:
    resolver = StaticBackendResolver(url_by_agent_and_service={})
    assert get_local_workspace_agent_ids(resolver) == ()


# -- tracker --


def test_tracker_set_state_fires_on_change_only_on_change() -> None:
    tracker = LocalMindLivenessTracker()
    agent = AgentId.generate()
    changes: list[tuple[str, LocalMindState]] = []
    tracker.add_on_change_callback(lambda aid, state: changes.append((str(aid), state)))

    tracker.set_state(agent, LocalMindState.RUNNING)
    tracker.set_state(agent, LocalMindState.RUNNING)  # no-op, same state
    tracker.set_state(agent, LocalMindState.STOPPED)

    assert changes == [(str(agent), LocalMindState.RUNNING), (str(agent), LocalMindState.STOPPED)]
    assert tracker.get_state(agent) == LocalMindState.STOPPED


def test_tracker_get_state_defaults_unknown() -> None:
    tracker = LocalMindLivenessTracker()
    assert tracker.get_state(AgentId.generate()) == LocalMindState.UNKNOWN


def test_tracker_apply_poll_results_diffs_and_drops_absent_agents() -> None:
    tracker = LocalMindLivenessTracker()
    a = AgentId.generate()
    b = AgentId.generate()
    changes: list[tuple[str, LocalMindState]] = []
    tracker.add_on_change_callback(lambda aid, state: changes.append((str(aid), state)))

    tracker.apply_poll_results({str(a): LocalMindState.RUNNING, str(b): LocalMindState.STOPPED})
    assert set(changes) == {(str(a), LocalMindState.RUNNING), (str(b), LocalMindState.STOPPED)}

    # Second poll: a unchanged (no event), b now running (event), and a dropped
    # agent leaves the snapshot entirely.
    changes.clear()
    tracker.apply_poll_results({str(a): LocalMindState.RUNNING, str(b): LocalMindState.RUNNING})
    assert changes == [(str(b), LocalMindState.RUNNING)]

    # b absent from the next snapshot -> dropped from tracking (no event), so a
    # fresh snapshot that omits it does not keep reporting it.
    changes.clear()
    tracker.apply_poll_results({str(a): LocalMindState.RUNNING})
    assert tracker.snapshot_all() == {a: LocalMindState.RUNNING}


def test_tracker_snapshot_all_returns_copy() -> None:
    tracker = LocalMindLivenessTracker()
    agent = AgentId.generate()
    tracker.set_state(agent, LocalMindState.RUNNING)

    snapshot = tracker.snapshot_all()
    snapshot[agent] = LocalMindState.STOPPED

    assert tracker.get_state(agent) == LocalMindState.RUNNING
