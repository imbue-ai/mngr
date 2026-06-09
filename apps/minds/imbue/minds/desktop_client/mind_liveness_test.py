from datetime import datetime
from datetime import timezone

from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.mind_liveness import MindLiveness
from imbue.minds.desktop_client.mind_liveness import classify_host_state
from imbue.minds.desktop_client.mind_liveness import compute_mind_liveness_by_agent_id
from imbue.minds.desktop_client.mind_liveness import get_shutdown_capable_workspace_agent_ids
from imbue.minds.desktop_client.mind_liveness import provider_backend_supports_shutdown
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


def _resolver_with_capable_agent(
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


# -- the single shutdown-capability gate --


def test_provider_backend_supports_shutdown_gates_on_local_backends() -> None:
    # Only the local backends currently expose host shutdown to minds.
    assert provider_backend_supports_shutdown("docker") is True
    assert provider_backend_supports_shutdown("lima") is True
    assert provider_backend_supports_shutdown("modal") is False
    assert provider_backend_supports_shutdown("ovh") is False


# -- host-state classification --


def test_classify_host_state_maps_running_stopped_unknown() -> None:
    assert classify_host_state(HostState.RUNNING) == MindLiveness.RUNNING
    # Every "container exists but is down" state collapses to STOPPED.
    assert classify_host_state(HostState.STOPPED) == MindLiveness.STOPPED
    assert classify_host_state(HostState.STOPPING) == MindLiveness.STOPPED
    assert classify_host_state(HostState.CRASHED) == MindLiveness.STOPPED
    assert classify_host_state(HostState.FAILED) == MindLiveness.STOPPED
    # Transient / unobserved states are UNKNOWN, not assumed stopped.
    assert classify_host_state(HostState.STARTING) == MindLiveness.UNKNOWN
    assert classify_host_state(HostState.PAUSED) == MindLiveness.UNKNOWN
    assert classify_host_state(None) == MindLiveness.UNKNOWN


# -- shutdown-capability classification from a resolver --


def test_get_shutdown_capable_workspace_agent_ids_keeps_only_capable_backends() -> None:
    resolver = MngrCliBackendResolver()
    capable_agent = AgentId.generate()
    remote_agent = AgentId.generate()
    resolver.update_providers(
        providers=(_provider("docker", "docker"), _provider("modal", "modal")),
        error_by_provider_name={},
        last_full_snapshot_at=datetime.now(timezone.utc),
    )
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(capable_agent, remote_agent),
            discovered_agents=(
                _workspace_agent(capable_agent, "docker"),
                _workspace_agent(remote_agent, "modal", host=_HOST_B),
            ),
        )
    )

    capable_ids = get_shutdown_capable_workspace_agent_ids(resolver)

    assert capable_ids == (capable_agent,)


def test_get_shutdown_capable_workspace_agent_ids_empty_for_non_mngr_resolver() -> None:
    resolver = StaticBackendResolver(url_by_agent_and_service={})
    assert get_shutdown_capable_workspace_agent_ids(resolver) == ()


# -- compute_mind_liveness_by_agent_id (over the resolver) --


def test_compute_reflects_discovery_host_state() -> None:
    agent = AgentId.generate()

    running = compute_mind_liveness_by_agent_id(_resolver_with_capable_agent(agent, HostState.RUNNING))
    assert running == {str(agent): MindLiveness.RUNNING}

    stopped = compute_mind_liveness_by_agent_id(_resolver_with_capable_agent(agent, HostState.STOPPED))
    assert stopped == {str(agent): MindLiveness.STOPPED}


def test_compute_unknown_when_host_state_absent() -> None:
    """Before discovery has the host's state, the mind is UNKNOWN (not assumed stopped)."""
    agent = AgentId.generate()
    states = compute_mind_liveness_by_agent_id(_resolver_with_capable_agent(agent, None))
    assert states == {str(agent): MindLiveness.UNKNOWN}


def test_compute_excludes_non_capable_minds() -> None:
    resolver = MngrCliBackendResolver()
    capable_agent = AgentId.generate()
    remote_agent = AgentId.generate()
    resolver.update_providers(
        providers=(_provider("docker", "docker"), _provider("modal", "modal")),
        error_by_provider_name={},
        last_full_snapshot_at=datetime.now(timezone.utc),
    )
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(capable_agent, remote_agent),
            discovered_agents=(
                _workspace_agent(capable_agent, "docker", host=_HOST_A),
                _workspace_agent(remote_agent, "modal", host=_HOST_B),
            ),
            host_state_by_host_id={str(_HOST_A): HostState.RUNNING, str(_HOST_B): HostState.RUNNING},
        )
    )

    states = compute_mind_liveness_by_agent_id(resolver)

    # Only the shutdown-capable (docker) mind is computed; the remote one never appears.
    assert states == {str(capable_agent): MindLiveness.RUNNING}


def test_compute_reflects_resolver_optimistic_override() -> None:
    """A Start/Stop override set on the resolver shows through compute ahead of discovery."""
    agent = AgentId.generate()
    resolver = _resolver_with_capable_agent(agent, HostState.RUNNING)

    # Discovery still says RUNNING; the resolver-level override flips it to STOPPED.
    resolver.set_host_state_override(_HOST_A, HostState.STOPPED)

    assert compute_mind_liveness_by_agent_id(resolver) == {str(agent): MindLiveness.STOPPED}
