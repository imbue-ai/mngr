"""TestClient coverage for the local-mind Start/Stop endpoints, the quit-prompt
running-minds lookup, the SSE payload helper, and the landing-page controls."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

from starlette.testclient import TestClient

from imbue.minds.desktop_client.app import _local_mind_state_payload
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.local_liveness import LocalMindLivenessTracker
from imbue.minds.desktop_client.local_liveness import LocalMindState
from imbue.mngr.api.discovery_events import make_discovered_provider
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName

_HOST = HostId("host-" + "0" * 31 + "1")


def _local_workspace_agent(agent_id: AgentId) -> DiscoveredAgent:
    return DiscoveredAgent(
        host_id=_HOST,
        agent_id=agent_id,
        agent_name=AgentName("ws-agent"),
        provider_name=ProviderInstanceName("docker"),
        certified_data={"labels": {"workspace": "my-workspace", "is_primary": "true"}},
    )


def _make_client(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    liveness_tracker: LocalMindLivenessTracker | None,
) -> tuple[TestClient, FileAuthStore]:
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        local_mind_liveness_tracker=liveness_tracker,
    )
    return TestClient(app, base_url="http://localhost"), auth_store


def _authenticate(client: TestClient, auth_store: FileAuthStore) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(signing_key=auth_store.get_signing_key()), path="/")


def _resolver_with_local_agent(agent_id: AgentId) -> MngrCliBackendResolver:
    resolver = MngrCliBackendResolver()
    resolver.update_providers(
        providers=(
            make_discovered_provider(
                ProviderInstanceName("docker"),
                ProviderInstanceConfig(backend=ProviderBackendName("docker"), is_enabled=True),
            ),
        ),
        error_by_provider_name={},
        last_full_snapshot_at=datetime.now(timezone.utc),
    )
    resolver.update_agents(
        ParsedAgentsResult(agent_ids=(agent_id,), discovered_agents=(_local_workspace_agent(agent_id),))
    )
    return resolver


# -- SSE payload helper --


def test_local_mind_state_payload_shape() -> None:
    agent = AgentId.generate()
    payload = _local_mind_state_payload(str(agent), LocalMindState.STOPPED)
    assert payload == {"type": "local_mind_state", "agent_id": str(agent), "state": "STOPPED"}


# -- endpoint auth + availability --


def test_stop_host_requires_authentication(tmp_path: Path) -> None:
    agent = AgentId.generate()
    client, _ = _make_client(tmp_path, _resolver_with_local_agent(agent), LocalMindLivenessTracker())
    response = client.post(f"/api/agents/{agent}/stop-host")
    assert response.status_code == 403


def test_start_host_requires_authentication(tmp_path: Path) -> None:
    agent = AgentId.generate()
    client, _ = _make_client(tmp_path, _resolver_with_local_agent(agent), LocalMindLivenessTracker())
    response = client.post(f"/api/agents/{agent}/start-host")
    assert response.status_code == 403


def test_stop_host_unavailable_without_concurrency_group(tmp_path: Path) -> None:
    """Without a concurrency group (test factory), the action can't be dispatched -> 503."""
    agent = AgentId.generate()
    client, auth_store = _make_client(tmp_path, _resolver_with_local_agent(agent), LocalMindLivenessTracker())
    _authenticate(client, auth_store)
    response = client.post(f"/api/agents/{agent}/stop-host")
    assert response.status_code == 503


def test_running_local_minds_requires_authentication(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path, MngrCliBackendResolver(), LocalMindLivenessTracker())
    response = client.get("/api/local-minds/running")
    assert response.status_code == 403


def test_running_local_minds_empty_without_concurrency_group(tmp_path: Path) -> None:
    """The quit-prompt lookup degrades to an empty list when it can't read state."""
    client, auth_store = _make_client(tmp_path, MngrCliBackendResolver(), LocalMindLivenessTracker())
    _authenticate(client, auth_store)
    response = client.get("/api/local-minds/running")
    assert response.status_code == 200
    assert response.json() == {"running": []}


# -- landing page integration --


def test_landing_page_shows_start_stop_for_local_mind(tmp_path: Path) -> None:
    """A local (docker) workspace renders the Start/Stop controls + seeded state, not Restart."""
    agent = AgentId.generate()
    resolver = _resolver_with_local_agent(agent)
    tracker = LocalMindLivenessTracker()
    tracker.set_state(agent, LocalMindState.STOPPED)
    client, auth_store = _make_client(tmp_path, resolver, tracker)
    _authenticate(client, auth_store)

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "landing-start-btn" in html
    assert "landing-stop-btn" in html
    # The seeded liveness is embedded so the page renders correct controls pre-SSE.
    assert "STOPPED" in html
