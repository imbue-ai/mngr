"""Flask test-client coverage for the mind host Start/Stop endpoints, the quit-prompt
running-minds lookup, the bulk stop-hosts endpoint, and the landing-page controls.

Mind liveness is derived from the discovery snapshot's host state (folded into the
resolver as ``host_state_by_host_id``) plus the resolver's optimistic
``set_host_state_override``; tests seed host state on the resolver rather than
poking a separate tracker.
"""

import re
from datetime import datetime
from datetime import timezone
from pathlib import Path

from flask.testing import FlaskClient

from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
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


def _capable_workspace_agent(agent_id: AgentId, host: HostId = _HOST_A) -> DiscoveredAgent:
    return DiscoveredAgent(
        host_id=host,
        agent_id=agent_id,
        agent_name=AgentName("ws-agent"),
        provider_name=ProviderInstanceName("docker"),
        certified_data={"labels": {"workspace": "my-workspace", "is_primary": "true"}},
    )


def _make_client(tmp_path: Path, resolver: MngrCliBackendResolver) -> tuple[FlaskClient, FileAuthStore]:
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
    )
    return app.test_client(), auth_store


def _authenticate(client: FlaskClient, auth_store: FileAuthStore) -> None:
    client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(signing_key=auth_store.get_signing_key()))


def _docker_provider() -> DiscoveredProvider:
    return make_discovered_provider(
        ProviderInstanceName("docker"),
        ProviderInstanceConfig(backend=ProviderBackendName("docker"), is_enabled=True),
    )


def _resolver_with_capable_agents(host_state_by_agent: dict[AgentId, HostState | None]) -> MngrCliBackendResolver:
    """Build a resolver carrying one docker workspace per entry, each on its own host.

    Each agent's host gets the supplied ``HostState`` (or none, to model "discovery
    has not learned the state yet"). At most two agents are supported (two hosts).
    """
    resolver = MngrCliBackendResolver()
    resolver.update_providers(
        providers=(_docker_provider(),),
        error_by_provider_name={},
        last_full_snapshot_at=datetime.now(timezone.utc),
    )
    hosts = (_HOST_A, _HOST_B)
    discovered: list[DiscoveredAgent] = []
    host_state_by_host_id: dict[str, HostState] = {}
    for index, (agent_id, host_state) in enumerate(host_state_by_agent.items()):
        host = hosts[index]
        discovered.append(_capable_workspace_agent(agent_id, host=host))
        if host_state is not None:
            host_state_by_host_id[str(host)] = host_state
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=tuple(host_state_by_agent),
            discovered_agents=tuple(discovered),
            host_state_by_host_id=host_state_by_host_id,
        )
    )
    return resolver


def _resolver_with_running_capable_agent(agent_id: AgentId) -> MngrCliBackendResolver:
    return _resolver_with_capable_agents({agent_id: HostState.RUNNING})


# -- endpoint auth + availability --


def test_stop_host_requires_authentication(tmp_path: Path) -> None:
    agent = AgentId.generate()
    client, _ = _make_client(tmp_path, _resolver_with_running_capable_agent(agent))
    response = client.post(f"/api/agents/{agent}/stop-host")
    assert response.status_code == 403


def test_start_host_requires_authentication(tmp_path: Path) -> None:
    agent = AgentId.generate()
    client, _ = _make_client(tmp_path, _resolver_with_running_capable_agent(agent))
    response = client.post(f"/api/agents/{agent}/start-host")
    assert response.status_code == 403


def test_stop_host_unavailable_without_concurrency_group(tmp_path: Path) -> None:
    """Without a concurrency group (test factory), the action can't run -> 503."""
    agent = AgentId.generate()
    client, auth_store = _make_client(tmp_path, _resolver_with_running_capable_agent(agent))
    _authenticate(client, auth_store)
    response = client.post(f"/api/agents/{agent}/stop-host")
    assert response.status_code == 503


def test_stop_mind_hosts_requires_authentication(tmp_path: Path) -> None:
    agent = AgentId.generate()
    client, _ = _make_client(tmp_path, _resolver_with_running_capable_agent(agent))
    response = client.post(f"/api/minds/stop-hosts?agent_id={agent}")
    assert response.status_code == 403


def test_stop_mind_hosts_unavailable_without_concurrency_group(tmp_path: Path) -> None:
    """The bulk stop runs ``mngr`` synchronously, so without a concurrency group it is 503."""
    agent = AgentId.generate()
    client, auth_store = _make_client(tmp_path, _resolver_with_running_capable_agent(agent))
    _authenticate(client, auth_store)
    response = client.post(f"/api/minds/stop-hosts?agent_id={agent}")
    assert response.status_code == 503


def test_running_minds_requires_authentication(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path, MngrCliBackendResolver())
    response = client.get("/api/minds/running")
    assert response.status_code == 403


def test_running_minds_empty_when_no_capable_minds(tmp_path: Path) -> None:
    """The quit-prompt lookup returns an empty list when discovery has no capable minds."""
    client, auth_store = _make_client(tmp_path, MngrCliBackendResolver())
    _authenticate(client, auth_store)
    response = client.get("/api/minds/running")
    assert response.status_code == 200
    assert response.get_json() == {"running": []}


def test_stop_state_container_requires_authentication(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path, MngrCliBackendResolver())
    response = client.post("/api/minds/stop-state-container")
    assert response.status_code == 403


def test_stop_state_container_noop_without_concurrency_group(tmp_path: Path) -> None:
    """Without a concurrency group (test factory) the state-container stop is a no-op."""
    client, auth_store = _make_client(tmp_path, MngrCliBackendResolver())
    _authenticate(client, auth_store)
    response = client.post("/api/minds/stop-state-container")
    assert response.status_code == 200
    assert response.get_json() == {"stopped": False}


def test_running_minds_reads_discovery_without_subprocess(tmp_path: Path) -> None:
    """The quit-prompt lookup returns running minds straight from discovery host state.

    No ``root_concurrency_group`` is wired here, so if the endpoint tried to shell
    out to ``mngr list`` it would degrade to empty; returning the running mind
    proves it reads the in-memory discovery state (instant, no subprocess).
    """
    running_agent = AgentId.generate()
    stopped_agent = AgentId.generate()
    resolver = _resolver_with_capable_agents({running_agent: HostState.RUNNING, stopped_agent: HostState.STOPPED})
    client, auth_store = _make_client(tmp_path, resolver)
    _authenticate(client, auth_store)

    response = client.get("/api/minds/running")

    assert response.status_code == 200
    running = response.get_json()["running"]
    # Only the RUNNING mind is listed; the STOPPED one is excluded.
    assert [entry["id"] for entry in running] == [str(running_agent)]


def test_running_minds_reflects_optimistic_override(tmp_path: Path) -> None:
    """A just-issued Stop override hides a still-RUNNING-in-discovery mind from the prompt."""
    agent = AgentId.generate()
    resolver = _resolver_with_running_capable_agent(agent)
    resolver.set_host_state_override(_HOST_A, HostState.STOPPED)
    client, auth_store = _make_client(tmp_path, resolver)
    _authenticate(client, auth_store)

    response = client.get("/api/minds/running")

    assert response.status_code == 200
    assert response.get_json() == {"running": []}


# -- landing page integration --


def _button_display(html: str, button_class: str) -> str:
    """Return the inline ``display`` value rendered on a landing control button.

    Returns ``"none"`` when the button is hidden, ``""`` when shown. Visibility is
    driven by inline ``display`` (not a ``.hidden`` class) because the button base
    class is ``inline-flex`` and would otherwise win and show both buttons.
    """
    match = re.search(button_class + r'[^>]*?style="([^"]*)"', html)
    assert match is not None, f"{button_class} not found with a style attribute"
    return "none" if "display:none" in match.group(1) else ""


def test_landing_page_stopped_mind_shows_only_start(tmp_path: Path) -> None:
    agent = AgentId.generate()
    resolver = _resolver_with_capable_agents({agent: HostState.STOPPED})
    client, auth_store = _make_client(tmp_path, resolver)
    _authenticate(client, auth_store)

    html = client.get("/").text

    # Exactly one control is visible: Start (the container is stopped), not Stop.
    assert _button_display(html, "landing-start-btn") == ""
    assert _button_display(html, "landing-stop-btn") == "none"
    assert "Restart workspace" not in html


def test_landing_page_running_mind_shows_only_stop(tmp_path: Path) -> None:
    agent = AgentId.generate()
    resolver = _resolver_with_capable_agents({agent: HostState.RUNNING})
    client, auth_store = _make_client(tmp_path, resolver)
    _authenticate(client, auth_store)

    html = client.get("/").text

    assert _button_display(html, "landing-stop-btn") == ""
    assert _button_display(html, "landing-start-btn") == "none"


def test_landing_page_unknown_mind_shows_neither_control(tmp_path: Path) -> None:
    """Before discovery knows the container state, neither Start nor Stop is shown."""
    agent = AgentId.generate()
    # No host state in discovery yet -> classified UNKNOWN.
    resolver = _resolver_with_capable_agents({agent: None})
    client, auth_store = _make_client(tmp_path, resolver)
    _authenticate(client, auth_store)

    html = client.get("/").text

    assert _button_display(html, "landing-start-btn") == "none"
    assert _button_display(html, "landing-stop-btn") == "none"
