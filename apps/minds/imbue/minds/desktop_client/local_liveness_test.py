from datetime import datetime
from datetime import timezone

from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.local_liveness import LocalMindState
from imbue.minds.desktop_client.local_liveness import LocalMindStateProvider
from imbue.minds.desktop_client.local_liveness import classify_host_state
from imbue.minds.desktop_client.local_liveness import get_local_workspace_agent_ids
from imbue.mngr.api.discovery_events import DiscoveredProvider
from imbue.mngr.api.discovery_events import make_discovered_provider
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName

_HOST_A = HostId("host-" + "0" * 31 + "1")
_HOST_B = HostId("host-" + "0" * 31 + "2")


def _provider(name: str, backend: str) -> DiscoveredProvider:
    return make_discovered_provider(
        ProviderInstanceName(name),
        ProviderInstanceConfig(backend=ProviderBackendName(backend), is_enabled=True),
    )


def _workspace_agent(agent_id: AgentId, provider_name: str, host: HostId = _HOST_A) -> DiscoveredAgent:
    return DiscoveredAgent(
        host_id=host,
        agent_id=agent_id,
        agent_name=AgentName("ws-agent"),
        provider_name=ProviderInstanceName(provider_name),
        certified_data={"labels": {"workspace": "my-workspace", "is_primary": "true"}},
    )


def _resolver_with_local_agent(
    agent_id: AgentId,
    host_state: HostState | None,
    host: HostId = _HOST_A,
) -> MngrCliBackendResolver:
    """Build a resolver carrying one docker-backed workspace whose host has ``host_state``."""
    resolver = MngrCliBackendResolver()
    resolver.update_providers(
        providers=(_provider("docker", "docker"),),
        error_by_provider_name={},
        last_full_snapshot_at=datetime.now(timezone.utc),
    )
    host_state_by_host_id = {str(host): host_state} if host_state is not None else {}
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(agent_id,),
            discovered_agents=(_workspace_agent(agent_id, "docker", host=host),),
            host_state_by_host_id=host_state_by_host_id,
        )
    )
    return resolver


# -- host-state classification --


def test_classify_host_state_maps_running_stopped_unknown() -> None:
    assert classify_host_state(HostState.RUNNING) == LocalMindState.RUNNING
    # Every "container exists but is down" state collapses to STOPPED.
    assert classify_host_state(HostState.STOPPED) == LocalMindState.STOPPED
    assert classify_host_state(HostState.STOPPING) == LocalMindState.STOPPED
    assert classify_host_state(HostState.CRASHED) == LocalMindState.STOPPED
    assert classify_host_state(HostState.FAILED) == LocalMindState.STOPPED
    # Transient / unobserved states are UNKNOWN, not assumed stopped.
    assert classify_host_state(HostState.STARTING) == LocalMindState.UNKNOWN
    assert classify_host_state(HostState.PAUSED) == LocalMindState.UNKNOWN
    assert classify_host_state(None) == LocalMindState.UNKNOWN


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
                _workspace_agent(remote_agent, "modal", host=_HOST_B),
            ),
        )
    )

    local_ids = get_local_workspace_agent_ids(resolver)

    assert local_ids == (local_agent,)


def test_get_local_workspace_agent_ids_empty_for_non_mngr_resolver() -> None:
    resolver = StaticBackendResolver(url_by_agent_and_service={})
    assert get_local_workspace_agent_ids(resolver) == ()


# -- LocalMindStateProvider: discovery-derived state --


def test_compute_reflects_discovery_host_state() -> None:
    agent = AgentId.generate()
    provider = LocalMindStateProvider()

    running = provider.compute_state_by_agent_id(_resolver_with_local_agent(agent, HostState.RUNNING))
    assert running == {str(agent): LocalMindState.RUNNING}

    stopped = provider.compute_state_by_agent_id(_resolver_with_local_agent(agent, HostState.STOPPED))
    assert stopped == {str(agent): LocalMindState.STOPPED}


def test_compute_unknown_when_host_state_absent() -> None:
    """Before discovery has the host's state, the mind is UNKNOWN (not assumed stopped)."""
    agent = AgentId.generate()
    provider = LocalMindStateProvider()
    states = provider.compute_state_by_agent_id(_resolver_with_local_agent(agent, None))
    assert states == {str(agent): LocalMindState.UNKNOWN}


def test_compute_excludes_remote_minds() -> None:
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
                _workspace_agent(local_agent, "docker", host=_HOST_A),
                _workspace_agent(remote_agent, "modal", host=_HOST_B),
            ),
            host_state_by_host_id={str(_HOST_A): HostState.RUNNING, str(_HOST_B): HostState.RUNNING},
        )
    )

    states = LocalMindStateProvider().compute_state_by_agent_id(resolver)

    # Only the local (docker) mind is computed; the remote one never appears.
    assert states == {str(local_agent): LocalMindState.RUNNING}


# -- LocalMindStateProvider: optimistic overrides --


def test_override_wins_over_discovery_until_discovery_agrees() -> None:
    agent = AgentId.generate()
    provider = LocalMindStateProvider()
    changes: list[None] = []
    provider.add_on_change_callback(lambda: changes.append(None))

    # Discovery still says RUNNING, but the user just stopped it: the override
    # makes the UI read STOPPED at once, and on-change fires to wake the SSE.
    provider.set_override(agent, LocalMindState.STOPPED)
    assert len(changes) == 1
    running_resolver = _resolver_with_local_agent(agent, HostState.RUNNING)
    assert provider.compute_state_by_agent_id(running_resolver) == {str(agent): LocalMindState.STOPPED}

    # Once discovery catches up (STOPPED), the override is confirmed and dropped.
    stopped_resolver = _resolver_with_local_agent(agent, HostState.STOPPED)
    assert provider.compute_state_by_agent_id(stopped_resolver) == {str(agent): LocalMindState.STOPPED}

    # With the override gone, discovery is authoritative again: a later restart
    # detected only by discovery is reflected without any override re-masking it.
    assert provider.compute_state_by_agent_id(running_resolver) == {str(agent): LocalMindState.RUNNING}


def test_clear_override_reverts_to_discovery() -> None:
    agent = AgentId.generate()
    provider = LocalMindStateProvider()
    provider.set_override(agent, LocalMindState.STOPPED)

    changes: list[None] = []
    provider.add_on_change_callback(lambda: changes.append(None))
    provider.clear_override(agent)
    assert len(changes) == 1

    running_resolver = _resolver_with_local_agent(agent, HostState.RUNNING)
    assert provider.compute_state_by_agent_id(running_resolver) == {str(agent): LocalMindState.RUNNING}


def test_clear_override_absent_is_noop() -> None:
    agent = AgentId.generate()
    provider = LocalMindStateProvider()
    changes: list[None] = []
    provider.add_on_change_callback(lambda: changes.append(None))
    # No override set -> clearing fires nothing.
    provider.clear_override(agent)
    assert changes == []


def test_override_pruned_when_mind_leaves_local_set() -> None:
    """An override for an agent no longer present in discovery is dropped, not retained."""
    agent = AgentId.generate()
    provider = LocalMindStateProvider()
    provider.set_override(agent, LocalMindState.STOPPED)

    # The agent is gone from discovery entirely (e.g. destroyed): it is absent
    # from the computed map, and the stale override must not linger.
    empty_resolver = MngrCliBackendResolver()
    empty_resolver.update_providers(
        providers=(_provider("docker", "docker"),),
        error_by_provider_name={},
        last_full_snapshot_at=datetime.now(timezone.utc),
    )
    assert provider.compute_state_by_agent_id(empty_resolver) == {}

    # If the same id reappears RUNNING, the pruned override does not resurrect.
    running_resolver = _resolver_with_local_agent(agent, HostState.RUNNING)
    assert provider.compute_state_by_agent_id(running_resolver) == {str(agent): LocalMindState.RUNNING}
