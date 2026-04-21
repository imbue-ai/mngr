from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi import Request as FastAPIRequest
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from pydantic import PrivateAttr
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.app import _build_workspace_list
from imbue.minds.desktop_client.app import _snapshot_stuck_for_chrome
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import DEFAULT_SERVER_NAME
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_server_log
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import create_sharing_request_event
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.ssh_tunnel import RemoteSSHInfo
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelError
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelManager
from imbue.minds.desktop_client.workspace_server_health import WorkspaceServerHealthTracker
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import ServerName
from imbue.mngr.primitives import AgentId


def _create_multi_backend_http_client(
    web_app: FastAPI,
    api_app: FastAPI,
) -> httpx.AsyncClient:
    """Create an httpx client that routes to different ASGI apps based on URL prefix.

    Requests to http://web-backend/... go to web_app, and
    requests to http://api-backend/... go to api_app.
    """
    web_transport = httpx.ASGITransport(app=web_app)
    api_transport = httpx.ASGITransport(app=api_app)

    class _RoutingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if str(request.url).startswith("http://web-backend"):
                return await web_transport.handle_async_request(request)
            elif str(request.url).startswith("http://api-backend"):
                return await api_transport.handle_async_request(request)
            else:
                raise httpx.ConnectError(f"Unknown backend: {request.url}")

    return httpx.AsyncClient(transport=_RoutingTransport())


def _create_test_backend() -> FastAPI:
    """Create a simple backend app for proxy testing."""
    backend = FastAPI()

    @backend.get("/")
    def backend_root() -> HTMLResponse:
        return HTMLResponse("<html><head><title>Backend</title></head><body>Hello from backend</body></html>")

    @backend.get("/api/status")
    def backend_status() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @backend.post("/api/echo")
    async def backend_echo(request: FastAPIRequest) -> JSONResponse:
        body = await request.body()
        return JSONResponse({"echo": body.decode()})

    return backend


def _create_test_desktop_client(
    tmp_path: Path,
    backend_resolver: BackendResolverInterface,
    http_client: httpx.AsyncClient | None,
    agent_creator: AgentCreator | None = None,
) -> tuple[TestClient, FileAuthStore]:
    """Create a desktop client with the given backend resolver."""
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=http_client,
        agent_creator=agent_creator,
    )
    client = TestClient(app)

    return client, auth_store


def _setup_test_server(
    tmp_path: Path,
    server_name: ServerName = DEFAULT_SERVER_NAME,
) -> tuple[TestClient, FileAuthStore, AgentId]:
    """Set up a desktop client with a test backend for proxy testing."""
    agent_id = AgentId()

    backend_app = _create_test_backend()
    test_http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backend_app),
        base_url="http://test-backend",
    )

    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={str(agent_id): {str(server_name): "http://test-backend"}},
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=test_http_client,
    )

    return client, auth_store, agent_id


def _authenticate_client(
    client: TestClient,
    auth_store: FileAuthStore,
) -> None:
    """Authenticate a test client by adding a one-time code and consuming it."""
    code = OneTimeCode("auth-code-{}".format(AgentId()))
    auth_store.add_one_time_code(code=code)
    client.get(
        "/authenticate",
        params={"one_time_code": str(code)},
        follow_redirects=False,
    )


def test_landing_page_shows_login_when_unauthenticated(tmp_path: Path) -> None:
    client, _, _ = _setup_test_server(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert "Login" in response.text


def test_login_redirects_to_authenticate_via_js(tmp_path: Path) -> None:
    client, auth_store, _ = _setup_test_server(tmp_path)
    code = OneTimeCode("login-code-{}".format(AgentId()))
    auth_store.add_one_time_code(code=code)

    response = client.get(
        "/login",
        params={"one_time_code": str(code)},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "window.location.href" in response.text
    assert "/authenticate" in response.text


def test_authenticate_with_valid_code_sets_cookie_and_redirects(tmp_path: Path) -> None:
    client, auth_store, _ = _setup_test_server(tmp_path)
    code = OneTimeCode("auth-code-{}".format(AgentId()))
    auth_store.add_one_time_code(code=code)

    response = client.get(
        "/authenticate",
        params={"one_time_code": str(code)},
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert SESSION_COOKIE_NAME in response.cookies


def test_authenticate_redirects_to_landing_page(tmp_path: Path) -> None:
    client, auth_store, _ = _setup_test_server(tmp_path)
    code = OneTimeCode("auth-code-{}".format(AgentId()))
    auth_store.add_one_time_code(code=code)

    response = client.get(
        "/authenticate",
        params={"one_time_code": str(code)},
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "/"


def test_authenticate_with_invalid_code_returns_403(tmp_path: Path) -> None:
    client, _, _ = _setup_test_server(tmp_path)

    response = client.get(
        "/authenticate",
        params={"one_time_code": "bogus-code-82734"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "invalid or has already been used" in response.text


def test_authenticate_code_cannot_be_reused(tmp_path: Path) -> None:
    client, auth_store, _ = _setup_test_server(tmp_path)
    code = OneTimeCode("once-only-{}".format(AgentId()))
    auth_store.add_one_time_code(code=code)

    first_response = client.get(
        "/authenticate",
        params={"one_time_code": str(code)},
        follow_redirects=False,
    )
    assert first_response.status_code == 307

    second_response = client.get(
        "/authenticate",
        params={"one_time_code": str(code)},
        follow_redirects=False,
    )
    assert second_response.status_code == 403


def test_landing_page_lists_single_agent(tmp_path: Path) -> None:
    """When authenticated and exactly one agent is known, the landing page lists it."""
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/")
    assert response.status_code == 200
    assert str(agent_id) in response.text


# -- Agent default redirect tests --


def test_agent_default_page_redirects_to_web_server(tmp_path: Path) -> None:
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={
            str(agent_id): {"web": "http://test-backend:9100"},
        },
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get(f"/forwarding/{agent_id}/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == f"/forwarding/{agent_id}/system_interface/"


def test_agent_default_page_rejects_unauthenticated_requests(tmp_path: Path) -> None:
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={str(agent_id): {"web": "http://test-backend"}},
    )
    client, _ = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.get(f"/forwarding/{agent_id}/", follow_redirects=False)
    assert response.status_code == 403


# -- Agent servers page tests --


def test_agent_servers_page_lists_available_servers(tmp_path: Path) -> None:
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={
            str(agent_id): {"web": "http://test-backend:9100", "api": "http://test-backend:9200"},
        },
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get(f"/forwarding/{agent_id}/servers/")
    assert response.status_code == 200
    assert "web" in response.text
    assert "api" in response.text
    assert f"/forwarding/{agent_id}/web/" in response.text
    assert f"/forwarding/{agent_id}/api/" in response.text


def test_agent_servers_page_shows_empty_state_when_no_servers(tmp_path: Path) -> None:
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={str(agent_id): {}})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get(f"/forwarding/{agent_id}/servers/")
    assert response.status_code == 200
    assert "No servers are currently running" in response.text


def test_agent_servers_page_rejects_unauthenticated_requests(tmp_path: Path) -> None:
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={str(agent_id): {"web": "http://test-backend"}},
    )
    client, _ = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.get(f"/forwarding/{agent_id}/servers/")
    assert response.status_code == 403


# -- Proxy tests (now with server_name in URL) --


def test_agent_proxy_rejects_unauthenticated_requests(tmp_path: Path) -> None:
    client, _, agent_id = _setup_test_server(tmp_path)

    response = client.get(f"/forwarding/{agent_id}/{DEFAULT_SERVER_NAME}/")
    assert response.status_code == 403


def test_agent_proxy_serves_bootstrap_on_first_navigation(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get(
        f"/forwarding/{agent_id}/{DEFAULT_SERVER_NAME}/",
        headers={
            "sec-fetch-mode": "navigate",
            "user-agent": "Mozilla/5.0 Electron/32.0.0",
        },
    )

    assert response.status_code == 200
    assert "serviceWorker.register" in response.text


def test_browser_navigation_serves_bootstrap_directly(tmp_path: Path) -> None:
    """Browser navigation now gets bootstrap page directly (no info bar wrapper)."""
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get(
        f"/forwarding/{agent_id}/{DEFAULT_SERVER_NAME}/",
        headers={
            "sec-fetch-mode": "navigate",
            "user-agent": "Mozilla/5.0 Firefox/130.0",
        },
    )

    assert response.status_code == 200
    assert "serviceWorker.register" in response.text


def test_agent_proxy_serves_service_worker_js(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get(f"/forwarding/{agent_id}/{DEFAULT_SERVER_NAME}/__sw.js")
    assert response.status_code == 200
    assert "application/javascript" in response.headers["content-type"]
    assert "skipWaiting" in response.text


def test_agent_proxy_forwards_get_request_to_backend(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)

    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")

    response = client.get(f"/forwarding/{agent_id}/{DEFAULT_SERVER_NAME}/api/status")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_agent_proxy_forwards_post_request_to_backend(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)

    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")

    response = client.post(
        f"/forwarding/{agent_id}/{DEFAULT_SERVER_NAME}/api/echo",
        content=b"test-body-content",
    )
    assert response.status_code == 200
    assert response.json() == {"echo": "test-body-content"}


def test_agent_proxy_injects_websocket_shim_into_html_responses(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)

    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")

    response = client.get(f"/forwarding/{agent_id}/{DEFAULT_SERVER_NAME}/")
    assert response.status_code == 200
    assert "OrigWebSocket" in response.text
    assert "Hello from backend" in response.text


def _setup_test_server_without_backend(
    tmp_path: Path,
) -> tuple[TestClient, FileAuthStore, AgentId]:
    """Set up a desktop client with no backends for testing error paths."""
    agent_id = AgentId()

    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    _authenticate_client(client=client, auth_store=auth_store)

    return client, auth_store, agent_id


def test_agent_proxy_returns_loading_page_for_unknown_backend(tmp_path: Path) -> None:
    client, _, agent_id = _setup_test_server_without_backend(tmp_path)

    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")

    response = client.get(
        f"/forwarding/{agent_id}/{DEFAULT_SERVER_NAME}/",
        headers={"Accept": "text/html"},
    )
    assert response.status_code == 200
    assert "Loading..." in response.text
    assert "location.reload()" in response.text
    # Convention links (terminal and agent) are always shown, even before those
    # servers have registered with the backend resolver.
    assert f"/forwarding/{agent_id}/terminal/" in response.text
    assert f"/forwarding/{agent_id}/agent/" in response.text
    assert 'target="_top"' in response.text


def test_agent_proxy_returns_502_for_unknown_backend_non_html(tmp_path: Path) -> None:
    client, _, agent_id = _setup_test_server_without_backend(tmp_path)

    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")

    response = client.get(
        f"/forwarding/{agent_id}/{DEFAULT_SERVER_NAME}/api/status",
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 502


def test_login_redirects_if_already_authenticated(tmp_path: Path) -> None:
    client, auth_store, _ = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)

    new_code = OneTimeCode("second-code-{}".format(AgentId()))
    auth_store.add_one_time_code(code=new_code)

    response = client.get(
        "/login",
        params={"one_time_code": str(new_code)},
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert response.headers["location"] == "/"


def test_websocket_proxy_rejects_unauthenticated_connection(tmp_path: Path) -> None:
    client, _, agent_id = _setup_test_server(tmp_path)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/forwarding/{agent_id}/{DEFAULT_SERVER_NAME}/ws"):
            pass

    assert exc_info.value.code == 4003


def test_websocket_proxy_rejects_unknown_backend(tmp_path: Path) -> None:
    client, _, agent_id = _setup_test_server_without_backend(tmp_path)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/forwarding/{agent_id}/{DEFAULT_SERVER_NAME}/ws"):
            pass

    assert exc_info.value.code == 4004


# -- Multi-server proxy tests --


def test_proxy_routes_to_correct_server_for_multi_server_agent(tmp_path: Path) -> None:
    """When an agent has multiple servers, each server_name routes to the correct backend."""
    agent_id = AgentId()

    # Create two distinct backends
    web_backend = FastAPI()

    @web_backend.get("/")
    def web_root() -> JSONResponse:
        return JSONResponse({"server": "web"})

    api_backend = FastAPI()

    @api_backend.get("/")
    def api_root() -> JSONResponse:
        return JSONResponse({"server": "api"})

    test_http_client = _create_multi_backend_http_client(web_app=web_backend, api_app=api_backend)

    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={
            str(agent_id): {
                "web": "http://web-backend",
                "api": "http://api-backend",
            },
        },
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=test_http_client,
    )

    _authenticate_client(client=client, auth_store=auth_store)
    client.cookies.set(f"sw_installed_{agent_id}_web", "1")
    client.cookies.set(f"sw_installed_{agent_id}_api", "1")

    web_response = client.get(f"/forwarding/{agent_id}/web/")
    assert web_response.status_code == 200
    assert web_response.json() == {"server": "web"}

    api_response = client.get(f"/forwarding/{agent_id}/api/")
    assert api_response.status_code == 200
    assert api_response.json() == {"server": "api"}


def test_agent_auth_covers_all_servers(tmp_path: Path) -> None:
    """Authenticating for an agent grants access to all of that agent's servers."""
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={
            str(agent_id): {
                "web": "http://test-backend",
                "api": "http://test-backend",
            },
        },
    )

    backend_app = _create_test_backend()
    test_http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backend_app),
        base_url="http://test-backend",
    )

    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=test_http_client,
    )

    # Not authenticated yet - both servers reject
    response_web = client.get(f"/forwarding/{agent_id}/web/")
    assert response_web.status_code == 403
    response_api = client.get(f"/forwarding/{agent_id}/api/")
    assert response_api.status_code == 403

    # Authenticate once (global session)
    _authenticate_client(client=client, auth_store=auth_store)

    client.cookies.set(f"sw_installed_{agent_id}_web", "1")
    client.cookies.set(f"sw_installed_{agent_id}_api", "1")

    # Both servers are now accessible
    response_web = client.get(f"/forwarding/{agent_id}/web/api/status")
    assert response_web.status_code == 200

    response_api = client.get(f"/forwarding/{agent_id}/api/api/status")
    assert response_api.status_code == 200


# -- Integration test: MngrCliBackendResolver with desktop client --


def test_mngr_cli_resolver_proxies_to_backend_discovered_via_mngr_cli(tmp_path: Path) -> None:
    """Full integration test: the MngrCliBackendResolver calls mngr CLI to discover
    the agent's server URL, and the desktop client proxies HTTP requests through."""
    agent_id = AgentId()
    data_dir = tmp_path / "minds_data"

    backend_app = _create_test_backend()
    test_http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backend_app),
        base_url="http://test-backend",
    )

    backend_resolver = make_resolver_with_data(
        server_logs={str(agent_id): make_server_log("web", "http://test-backend")},
        agents_json=make_agents_json(agent_id),
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=data_dir,
        backend_resolver=backend_resolver,
        http_client=test_http_client,
    )

    assert backend_resolver.get_backend_url(agent_id, ServerName("web")) == "http://test-backend"
    assert agent_id in backend_resolver.list_known_agent_ids()

    _authenticate_client(client=client, auth_store=auth_store)
    client.cookies.set(f"sw_installed_{agent_id}_web", "1")

    response = client.get(f"/forwarding/{agent_id}/web/api/status")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    response = client.post(
        f"/forwarding/{agent_id}/web/api/echo",
        content=b"integration-test",
    )
    assert response.status_code == 200
    assert response.json() == {"echo": "integration-test"}


def test_mngr_cli_resolver_multi_server_integration(tmp_path: Path) -> None:
    """Integration test: MngrCliBackendResolver with multiple servers per agent."""
    agent_id = AgentId()
    data_dir = tmp_path / "minds_data"

    log_content = make_server_log("web", "http://web-backend") + make_server_log("api", "http://api-backend")

    # Create distinct backends for web and api
    web_backend = FastAPI()

    @web_backend.get("/health")
    def web_health() -> JSONResponse:
        return JSONResponse({"source": "web"})

    api_backend = FastAPI()

    @api_backend.get("/health")
    def api_health() -> JSONResponse:
        return JSONResponse({"source": "api"})

    test_http_client = _create_multi_backend_http_client(web_app=web_backend, api_app=api_backend)

    backend_resolver = make_resolver_with_data(
        server_logs={str(agent_id): log_content},
        agents_json=make_agents_json(agent_id),
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=data_dir,
        backend_resolver=backend_resolver,
        http_client=test_http_client,
    )

    # Verify resolver sees both servers
    servers = backend_resolver.list_servers_for_agent(agent_id)
    assert ServerName("web") in servers
    assert ServerName("api") in servers

    _authenticate_client(client=client, auth_store=auth_store)
    client.cookies.set(f"sw_installed_{agent_id}_web", "1")
    client.cookies.set(f"sw_installed_{agent_id}_api", "1")

    # Verify each server routes correctly
    web_response = client.get(f"/forwarding/{agent_id}/web/health")
    assert web_response.status_code == 200
    assert web_response.json() == {"source": "web"}

    api_response = client.get(f"/forwarding/{agent_id}/api/health")
    assert api_response.status_code == 200
    assert api_response.json() == {"source": "api"}


def test_mngr_cli_resolver_returns_loading_page_when_backend_unavailable(tmp_path: Path) -> None:
    """When backend is not available, the proxy returns a loading page that retries client-side."""
    agent_id = AgentId()
    data_dir = tmp_path / "minds_data"

    backend_resolver = MngrCliBackendResolver()
    client, auth_store = _create_test_desktop_client(
        tmp_path=data_dir,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    _authenticate_client(client=client, auth_store=auth_store)
    client.cookies.set(f"sw_installed_{agent_id}_web", "1")

    response = client.get(f"/forwarding/{agent_id}/web/", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "Loading..." in response.text
    assert "location.reload()" in response.text


def test_mngr_cli_resolver_landing_page_lists_single_discovered_agent(tmp_path: Path) -> None:
    """When a single agent is discovered and authenticated, the landing page lists it."""
    agent_id = AgentId()
    data_dir = tmp_path / "minds_data"

    backend_resolver = make_resolver_with_data(
        server_logs={str(agent_id): make_server_log("web", "http://test-backend")},
        agents_json=make_agents_json(agent_id),
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=data_dir,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/")
    assert response.status_code == 200
    assert str(agent_id) in response.text


def test_mngr_cli_resolver_agent_servers_page_via_mngr_cli(tmp_path: Path) -> None:
    """The agent servers page lists servers discovered via mngr events."""
    agent_id = AgentId()
    data_dir = tmp_path / "minds_data"

    log_content = make_server_log("web", "http://test:9100") + make_server_log("api", "http://test:9200")

    backend_resolver = make_resolver_with_data(
        server_logs={str(agent_id): log_content},
        agents_json=make_agents_json(agent_id),
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=data_dir,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get(f"/forwarding/{agent_id}/servers/")
    assert response.status_code == 200
    assert "web" in response.text
    assert "api" in response.text


# -- SSH tunnel error handling tests --


class _FailingTunnelManager(SSHTunnelManager):
    """Tunnel manager that raises SSHTunnelError on every tunnel request."""

    def get_tunnel_socket_path(
        self,
        ssh_info: RemoteSSHInfo,
        remote_host: str,
        remote_port: int,
    ) -> Path:
        raise SSHTunnelError("SSH connection failed: test error")


class _RemoteStaticBackendResolver(StaticBackendResolver):
    """StaticBackendResolver that also returns SSH info for all known agents."""

    ssh_info: RemoteSSHInfo

    def get_ssh_info(self, agent_id: AgentId) -> RemoteSSHInfo | None:
        if self.url_by_agent_and_server.get(str(agent_id)) is not None:
            return self.ssh_info
        return None


_TEST_SSH_INFO: RemoteSSHInfo = RemoteSSHInfo(
    user="root",
    host="remote.example.com",
    port=22,
    key_path=Path("/tmp/fake_key"),
)


def _setup_failing_tunnel_server(
    tmp_path: Path,
) -> tuple[TestClient, FileAuthStore, AgentId]:
    """Set up a desktop client with a tunnel manager that always fails."""
    agent_id = AgentId()
    backend_resolver = _RemoteStaticBackendResolver(
        url_by_agent_and_server={str(agent_id): {"web": "http://127.0.0.1:9100"}},
        ssh_info=_TEST_SSH_INFO,
    )
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        tunnel_manager=_FailingTunnelManager(),
    )
    client = TestClient(app)
    _authenticate_client(client=client, auth_store=auth_store)
    return client, auth_store, agent_id


def test_http_proxy_returns_502_when_ssh_tunnel_fails(tmp_path: Path) -> None:
    """When SSH tunnel setup fails, the HTTP proxy should return 502 not 500."""
    client, _, agent_id = _setup_failing_tunnel_server(tmp_path)
    client.cookies.set(f"sw_installed_{agent_id}_web", "1")

    response = client.get(f"/forwarding/{agent_id}/web/api/status")
    assert response.status_code == 502
    assert "SSH tunnel" in response.text


def test_websocket_proxy_closes_with_1011_when_ssh_tunnel_fails(tmp_path: Path) -> None:
    """When SSH tunnel setup fails, the WebSocket should close with code 1011."""
    client, _, agent_id = _setup_failing_tunnel_server(tmp_path)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/forwarding/{agent_id}/web/ws"):
            pass

    assert exc_info.value.code == 1011


def test_http_proxy_without_tunnel_manager_works_for_local_backend(tmp_path: Path) -> None:
    """When no tunnel_manager is provided, local backends work normally."""
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)
    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")

    response = client.get(f"/forwarding/{agent_id}/{DEFAULT_SERVER_NAME}/api/status")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# -- Backend not-yet-ready handling tests --


class _DisconnectingTransport(httpx.AsyncBaseTransport):
    """Transport that always raises RemoteProtocolError, simulating a backend
    that accepted the TCP connection but hung up before sending any HTTP data.

    This is what httpx surfaces when an SSH tunnel forwards to a port whose
    server hasn't finished binding yet (e.g. uvicorn still starting up): the
    SSH channel-open to the backend port fails and the tunnel relay closes
    the local socket without writing anything.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)


def _setup_disconnecting_backend_server(
    tmp_path: Path,
) -> tuple[TestClient, FileAuthStore, AgentId]:
    """Set up a desktop client whose backend always raises RemoteProtocolError."""
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={str(agent_id): {str(DEFAULT_SERVER_NAME): "http://test-backend"}},
    )
    http_client = httpx.AsyncClient(transport=_DisconnectingTransport(), base_url="http://test-backend")
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=http_client,
    )
    _authenticate_client(client=client, auth_store=auth_store)
    return client, auth_store, agent_id


def test_http_proxy_returns_loading_page_when_backend_disconnects_html(tmp_path: Path) -> None:
    """When the backend hangs up without sending a response, HTML requests get the loading page."""
    client, _, agent_id = _setup_disconnecting_backend_server(tmp_path)
    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")

    response = client.get(
        f"/forwarding/{agent_id}/{DEFAULT_SERVER_NAME}/",
        headers={"Accept": "text/html"},
    )
    assert response.status_code == 200
    assert "Loading..." in response.text
    assert "location.reload()" in response.text


def test_http_proxy_returns_502_when_backend_disconnects_non_html(tmp_path: Path) -> None:
    """When the backend hangs up without sending a response, non-HTML requests get a 502."""
    client, _, agent_id = _setup_disconnecting_backend_server(tmp_path)
    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")

    response = client.get(
        f"/forwarding/{agent_id}/{DEFAULT_SERVER_NAME}/api/status",
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 502


# -- Backend URL with query string tests --


def test_proxy_combines_stored_and_request_query_strings(tmp_path: Path) -> None:
    """When backend URL has a query string (?arg=chat), it combines with request query params."""
    agent_id = AgentId()

    # Backend that echoes the full request URL query string
    backend_app = FastAPI()

    @backend_app.get("/")
    def echo_root(request: FastAPIRequest) -> JSONResponse:
        return JSONResponse({"query": str(request.url.query)})

    @backend_app.get("/{path:path}")
    def echo_path(request: FastAPIRequest, path: str) -> JSONResponse:
        return JSONResponse({"path": path, "query": str(request.url.query)})

    test_http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backend_app),
        base_url="http://test-backend",
    )

    # Register backend with query string in the URL (like ttyd ?arg=chat dispatch)
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={
            str(agent_id): {"chat": "http://test-backend?arg=chat"},
        },
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=test_http_client,
    )
    _authenticate_client(client=client, auth_store=auth_store)
    client.cookies.set(f"sw_installed_{agent_id}_chat", "1")

    # Request with no additional query -- only stored query should arrive
    response = client.get(f"/forwarding/{agent_id}/chat/")
    assert response.status_code == 200
    assert response.json()["query"] == "arg=chat"

    # Request with additional query -- both should be combined
    response = client.get(f"/forwarding/{agent_id}/chat/", params={"arg": "CONV123"})
    assert response.status_code == 200
    query = response.json()["query"]
    assert "arg=chat" in query
    assert "arg=CONV123" in query


def test_proxy_works_with_backend_url_without_query_string(tmp_path: Path) -> None:
    """Backend URLs without query strings still work correctly (regression test)."""
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)
    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")

    # Existing test: plain backend URL with request query
    response = client.get(f"/forwarding/{agent_id}/{DEFAULT_SERVER_NAME}/api/status", params={"foo": "bar"})
    assert response.status_code == 200


# -- Landing page agent creation tests --


def test_landing_page_shows_discovering_when_initial_discovery_not_done(tmp_path: Path) -> None:
    """Before initial discovery completes, show discovering state with auto-refresh."""
    backend_resolver = MngrCliBackendResolver()
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/")
    assert response.status_code == 200
    assert "Discovering agents" in response.text
    assert "reload" in response.text


def test_landing_page_shows_create_form_after_discovery_finds_no_agents(tmp_path: Path) -> None:
    """After discovery completes with no agents, show the create form."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/")
    assert response.status_code == 200
    assert "Create a Project" in response.text
    assert "git_url" in response.text


def test_landing_page_prefills_git_url_from_query_param(tmp_path: Path) -> None:
    """The create form pre-fills the git URL from a query parameter."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/", params={"git_url": "file:///nonexistent-repo"})
    assert response.status_code == 200
    assert "file:///nonexistent-repo" in response.text


def test_create_page_shows_form(tmp_path: Path) -> None:
    """GET /create shows the agent creation form."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/create")
    assert response.status_code == 200
    assert "Create a Project" in response.text


def test_creation_status_returns_404_for_unknown_agent(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/status returns 404 for unknown creation."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    unknown_id = AgentId()
    response = client.get("/api/create-agent/{}/status".format(unknown_id))
    assert response.status_code == 404


def test_landing_page_lists_agents_when_multiple_known(tmp_path: Path) -> None:
    """When authenticated and multiple agents are known, the landing page lists them all."""
    agent_id_1 = AgentId()
    agent_id_2 = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={
            str(agent_id_1): {"web": "http://test:9100"},
            str(agent_id_2): {"web": "http://test:9200"},
        },
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/")
    assert response.status_code == 200
    assert str(agent_id_1) in response.text
    assert str(agent_id_2) in response.text


def test_create_form_submit_returns_501_without_agent_creator(tmp_path: Path) -> None:
    """POST /create returns 501 when no agent_creator is configured."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.post("/create", data={"git_url": "file:///nonexistent-repo"})
    assert response.status_code == 501


def test_create_agent_api_returns_501_without_agent_creator(tmp_path: Path) -> None:
    """POST /api/create-agent returns 501 when no agent_creator is configured."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.post("/api/create-agent", json={"git_url": "file:///nonexistent-repo"})
    assert response.status_code == 501


def test_creating_page_returns_501_without_agent_creator(tmp_path: Path) -> None:
    """GET /creating/{id} returns 501 when no agent_creator is configured."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    agent_id = AgentId()
    response = client.get("/creating/{}".format(agent_id))
    assert response.status_code == 501


def _create_test_server_with_agent_creator(
    tmp_path: Path,
) -> tuple[TestClient, FileAuthStore, AgentCreator]:
    """Create a desktop client with an agent creator for testing.

    The returned client is already authenticated with a global session.
    """
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    agent_creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
        agent_creator=agent_creator,
    )
    _authenticate_client(client=client, auth_store=auth_store)
    return client, auth_store, agent_creator


def test_create_form_submit_redirects_to_creating_page(tmp_path: Path) -> None:
    """POST /create with valid git_url redirects to /creating/{agent_id}."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/create",
        data={"git_url": "file:///nonexistent-repo"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/creating/")
    agent_creator.wait_for_all()


def test_create_form_submit_rejects_empty_git_url(tmp_path: Path) -> None:
    """POST /create with empty git_url returns 400."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post("/create", data={"git_url": "", "agent_name": "test"})
    assert response.status_code == 400


def test_create_form_submit_passes_agent_name(tmp_path: Path) -> None:
    """POST /create passes agent_name to the creator."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/create",
        data={"git_url": "file:///nonexistent-repo", "agent_name": "my-agent"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    agent_creator.wait_for_all()


def test_create_agent_api_passes_agent_name(tmp_path: Path) -> None:
    """POST /api/create-agent passes agent_name to the creator."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/api/create-agent",
        json={"git_url": "file:///nonexistent-repo", "agent_name": "my-agent"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "agent_id" in data
    agent_creator.wait_for_all()


def test_create_agent_api_returns_agent_id(tmp_path: Path) -> None:
    """POST /api/create-agent returns JSON with agent_id and status."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    response = client.post("/api/create-agent", json={"git_url": "file:///nonexistent-repo"})
    assert response.status_code == 200
    data = response.json()
    assert "agent_id" in data
    assert data["status"] == "CLONING"
    agent_creator.wait_for_all()


def test_create_agent_api_rejects_empty_git_url(tmp_path: Path) -> None:
    """POST /api/create-agent with empty git_url returns 400."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post("/api/create-agent", json={"git_url": ""})
    assert response.status_code == 400


def test_create_agent_api_rejects_invalid_json(tmp_path: Path) -> None:
    """POST /api/create-agent with invalid JSON returns 400."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/api/create-agent",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert "Invalid JSON" in response.text


def test_creating_page_shows_status(tmp_path: Path) -> None:
    """GET /creating/{agent_id} shows the creating progress page."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    agent_id = agent_creator.start_creation("file:///nonexistent-repo")

    response = client.get("/creating/{}".format(agent_id))
    assert response.status_code == 200
    assert "Creating your project" in response.text
    agent_creator.wait_for_all()


def test_creating_page_returns_404_for_unknown(tmp_path: Path) -> None:
    """GET /creating/{agent_id} returns 404 for unknown agent creation."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.get("/creating/{}".format(AgentId()))
    assert response.status_code == 404


def test_creation_status_api_returns_status_for_tracked_agent(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/status returns a valid status for a tracked creation."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    agent_id = agent_creator.start_creation("file:///nonexistent-repo")

    response = client.get("/api/create-agent/{}/status".format(agent_id))
    assert response.status_code == 200
    data = response.json()
    assert data["agent_id"] == str(agent_id)
    assert data["status"] in ("CLONING", "CREATING", "DONE", "FAILED")
    agent_creator.wait_for_all()


def test_create_page_prefills_git_url_from_query(tmp_path: Path) -> None:
    """GET /create?git_url=... pre-fills the form."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.get("/create", params={"git_url": "file:///nonexistent-repo"})
    assert response.status_code == 200
    assert "file:///nonexistent-repo" in response.text


def test_landing_page_shows_create_link_when_multiple_agents_known(tmp_path: Path) -> None:
    """When authenticated with multiple agents known, landing page shows a 'Create' link."""
    agent_id_1 = AgentId()
    agent_id_2 = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={
            str(agent_id_1): {"web": "http://test:9100"},
            str(agent_id_2): {"web": "http://test:9200"},
        },
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/")
    assert response.status_code == 200
    assert "/create" in response.text


def test_create_page_rejects_unauthenticated(tmp_path: Path) -> None:
    """GET /create returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, _ = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.get("/create")
    assert response.status_code == 403


def test_create_form_submit_rejects_unauthenticated(tmp_path: Path) -> None:
    """POST /create returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, _ = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.post("/create", data={"git_url": "file:///nonexistent-repo"})
    assert response.status_code == 403


def test_create_agent_api_rejects_unauthenticated(tmp_path: Path) -> None:
    """POST /api/create-agent returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, _ = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.post("/api/create-agent", json={"git_url": "file:///nonexistent-repo"})
    assert response.status_code == 403


def test_creation_status_api_rejects_unauthenticated(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/status returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, _ = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.get("/api/create-agent/{}/status".format(AgentId()))
    assert response.status_code == 403


def test_creation_logs_sse_returns_501_without_agent_creator(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/logs returns 501 when no agent_creator."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/api/create-agent/{}/logs".format(AgentId()))
    assert response.status_code == 501


def test_creation_logs_sse_rejects_unauthenticated(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/logs returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, _ = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.get("/api/create-agent/{}/logs".format(AgentId()))
    assert response.status_code == 403


def test_creation_logs_sse_returns_404_for_unknown(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/logs returns 404 for unknown agent."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.get("/api/create-agent/{}/logs".format(AgentId()))
    assert response.status_code == 404


def test_creation_logs_sse_streams_events(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/logs returns SSE stream for a tracked creation."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    agent_id = agent_creator.start_creation("file:///nonexistent-repo")

    with client.stream("GET", "/api/create-agent/{}/logs".format(agent_id)) as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
    agent_creator.wait_for_all()


def test_creating_page_rejects_unauthenticated(tmp_path: Path) -> None:
    """GET /creating/{id} returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, _ = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.get("/creating/{}".format(AgentId()))
    assert response.status_code == 403


def test_create_form_submit_passes_launch_mode(tmp_path: Path) -> None:
    """POST /create passes launch_mode to the creator."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/create",
        data={
            "git_url": "file:///nonexistent-repo",
            "agent_name": "my-agent",
            "launch_mode": "DEV",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    agent_creator.wait_for_all()


def test_create_agent_api_passes_launch_mode(tmp_path: Path) -> None:
    """POST /api/create-agent passes launch_mode to the creator."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/api/create-agent",
        json={
            "git_url": "file:///nonexistent-repo",
            "agent_name": "my-agent",
            "launch_mode": "DEV",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "agent_id" in data
    agent_creator.wait_for_all()


def test_create_agent_api_rejects_invalid_launch_mode(tmp_path: Path) -> None:
    """POST /api/create-agent returns 400 for an invalid launch_mode."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/api/create-agent",
        json={
            "git_url": "file:///nonexistent-repo",
            "agent_name": "my-agent",
            "launch_mode": "INVALID_MODE",
        },
    )
    assert response.status_code == 400
    assert "Invalid launch_mode" in response.json()["error"]


def test_create_form_shows_launch_mode_dropdown(tmp_path: Path) -> None:
    """GET /create form includes the launch mode dropdown."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.get("/create")
    assert response.status_code == 200
    assert "launch_mode" in response.text
    assert "local" in response.text
    assert "cloud" in response.text
    assert "dev" in response.text


def test_unhandled_exception_returns_500_with_message(tmp_path: Path) -> None:
    """Unhandled exceptions in routes produce a 500 response with the error message."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    @app.get("/explode")
    def explode() -> None:
        raise RuntimeError("test boom")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/explode")
    assert response.status_code == 500
    assert "test boom" in response.text


# -- Chrome routes --


def test_chrome_page_renders_without_auth(tmp_path: Path) -> None:
    """The /_chrome route is unauthenticated and returns the chrome HTML."""
    client, _, _ = _setup_test_server(tmp_path)

    response = client.get("/_chrome")
    assert response.status_code == 200
    assert "minds-titlebar" in response.text
    assert "content-frame" in response.text


def test_chrome_page_includes_sidebar_toggle(tmp_path: Path) -> None:
    client, _, _ = _setup_test_server(tmp_path)

    response = client.get("/_chrome")
    assert response.status_code == 200
    assert "sidebar-toggle" in response.text
    assert "sidebar-panel" in response.text


def test_chrome_sidebar_page_renders(tmp_path: Path) -> None:
    """The /_chrome/sidebar route returns the standalone sidebar HTML."""
    client, _, _ = _setup_test_server(tmp_path)

    response = client.get("/_chrome/sidebar")
    assert response.status_code == 200
    assert "sidebar-workspaces" in response.text
    assert "EventSource" in response.text


def test_chrome_events_sse_returns_auth_required_when_unauthenticated(tmp_path: Path) -> None:
    """The /_chrome/events SSE endpoint returns auth_required for unauthenticated users."""
    client, _, _ = _setup_test_server(tmp_path)

    response = client.get("/_chrome/events")
    assert response.status_code == 200
    assert "auth_required" in response.text


def test_chrome_events_sse_returns_workspaces_when_authenticated(tmp_path: Path) -> None:
    """The /_chrome/events SSE endpoint returns workspace list for authenticated users.

    We test the underlying _build_workspace_list helper since the SSE endpoint
    is an infinite stream that the TestClient cannot consume without blocking.
    """
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={str(agent_id): {str(DEFAULT_SERVER_NAME): "http://test-backend"}},
    )

    workspaces = _build_workspace_list(backend_resolver)
    assert len(workspaces) == 1
    assert workspaces[0]["id"] == str(agent_id)


# -- Tests for new account management and request routes --


def _create_test_client_with_stores(
    tmp_path: Path,
) -> tuple[TestClient, FileAuthStore]:
    """Create a desktop client with session store and config for testing new routes."""
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    session_store = MultiAccountSessionStore(data_dir=tmp_path)
    minds_config = MindsConfig(data_dir=tmp_path)
    request_inbox = RequestInbox()

    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        session_store=session_store,
        minds_config=minds_config,
        request_inbox=request_inbox,
        paths=WorkspacePaths(data_dir=tmp_path),
    )
    client = TestClient(app)
    return client, auth_store


def test_accounts_page_requires_auth(tmp_path: Path) -> None:
    """The /accounts page requires authentication."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/accounts")
    assert response.status_code == 403


def test_accounts_page_shows_empty_when_no_accounts(tmp_path: Path) -> None:
    """The /accounts page shows no accounts when none are logged in."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.get("/accounts")
    assert response.status_code == 200
    assert "No accounts logged in" in response.text


def test_accounts_page_shows_logged_in_accounts(tmp_path: Path) -> None:
    """The /accounts page lists logged-in accounts."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)

    session_store = MultiAccountSessionStore(data_dir=tmp_path)
    session_store.add_or_update_session(
        access_token="tok-123",
        refresh_token=None,
        user_id="user-test-123",
        email="test@example.com",
    )

    response = client.get("/accounts")
    assert response.status_code == 200
    assert "test@example.com" in response.text


def test_workspace_settings_page_requires_auth(tmp_path: Path) -> None:
    """The workspace settings page requires authentication."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/workspace/agent-123/settings")
    assert response.status_code == 403


def test_workspace_settings_shows_unassociated_workspace(tmp_path: Path) -> None:
    """A workspace not associated with any account shows the associate prompt."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    test_agent_id = AgentId()
    response = client.get(f"/workspace/{test_agent_id}/settings")
    assert response.status_code == 200
    assert "associated with an account" in response.text.lower()


def test_requests_panel_requires_auth(tmp_path: Path) -> None:
    """The requests panel requires authentication."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/_chrome/requests-panel")
    assert response.status_code == 200
    assert "Not authenticated" in response.text


def test_requests_panel_shows_empty_inbox(tmp_path: Path) -> None:
    """The requests panel shows no pending requests when inbox is empty."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.get("/_chrome/requests-panel")
    assert response.status_code == 200
    assert "Requests (0)" in response.text


def test_requests_panel_card_routes_via_minds_bridge(tmp_path: Path) -> None:
    """A pending request renders a card whose onclick calls navigateToRequest
    with both event_id and agent_id, and the inline script prefers the
    window.minds.navigateToRequest bridge when available."""
    # Build the app inline so we can seed the inbox before creating the
    # TestClient and still have a concretely-typed handle to app.state.
    agent_id = str(AgentId())
    event = create_sharing_request_event(agent_id=agent_id, server_name="web")
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    session_store = MultiAccountSessionStore(data_dir=tmp_path)
    minds_config = MindsConfig(data_dir=tmp_path)
    request_inbox = RequestInbox().add_request(event)
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        session_store=session_store,
        minds_config=minds_config,
        request_inbox=request_inbox,
        paths=WorkspacePaths(data_dir=tmp_path),
    )
    client = TestClient(app)
    _authenticate_client(client, auth_store)

    response = client.get("/_chrome/requests-panel")
    assert response.status_code == 200
    body = response.text

    # The rendered card must reference both ids in its onclick.
    assert "navigateToRequest" in body
    assert str(event.event_id) in body
    assert agent_id in body
    # Defense-in-depth escaping: ids are embedded via JSON/HTML-escaped quotes
    # rather than raw single quotes, so &quot; must appear in place of ".
    assert f"&quot;{event.event_id}&quot;" in body
    assert f"&quot;{agent_id}&quot;" in body

    # The script must prefer the IPC bridge when present, and keep the
    # in-window and top-level fallbacks.
    assert "window.minds.navigateToRequest" in body
    assert "window.minds.navigateContent" in body
    assert "window.top.location" in body


def test_request_page_not_found(tmp_path: Path) -> None:
    """Requesting a non-existent request ID returns 404."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.get("/requests/nonexistent-id")
    assert response.status_code == 404


def test_set_default_account(tmp_path: Path) -> None:
    """Setting a default account works correctly."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.post(
        "/accounts/set-default",
        data={"user_id": "user-default-123"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    config = MindsConfig(data_dir=tmp_path)
    assert config.get_default_account_id() == "user-default-123"


def test_auto_open_toggle(tmp_path: Path) -> None:
    """The auto-open requests panel setting can be toggled."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.post(
        "/_chrome/requests-auto-open",
        json={"enabled": False},
    )
    assert response.status_code == 200

    config = MindsConfig(data_dir=tmp_path)
    assert config.get_auto_open_requests_panel() is False


# -- Restart workspace server endpoint tests --


class _RestartBackendResolver(StaticBackendResolver):
    """Static resolver that also returns ssh_info, workspace_name, and work_dir for tests.

    If ssh_info is None (simulates a local mind), get_ssh_info returns None but
    the other lookups still work from their per-agent maps.
    """

    ssh_info: RemoteSSHInfo | None = None
    workspace_name_by_agent_id: Mapping[str, str]
    work_dir_by_agent_id: Mapping[str, Path] = {}

    def get_ssh_info(self, agent_id: AgentId) -> RemoteSSHInfo | None:
        if self.ssh_info is None:
            return None
        if self.url_by_agent_and_server.get(str(agent_id)) is not None:
            return self.ssh_info
        return None

    def get_workspace_name(self, agent_id: AgentId) -> str | None:
        return self.workspace_name_by_agent_id.get(str(agent_id))

    def get_work_dir(self, agent_id: AgentId) -> Path | None:
        return self.work_dir_by_agent_id.get(str(agent_id))


class _RecordingTunnelManager(SSHTunnelManager):
    """Tunnel manager that records exec_remote_command calls and returns a configurable result.

    Use configure(...) to control what exec_remote_command returns or raises.
    """

    _exec_calls: list[tuple[RemoteSSHInfo, str]] = PrivateAttr(default_factory=list)
    _exec_exit_status: int = PrivateAttr(default=0)
    _exec_stderr: str = PrivateAttr(default="")
    _exec_raise_type: type[Exception] | None = PrivateAttr(default=None)

    def configure(
        self,
        exec_exit_status: int = 0,
        exec_stderr: str = "",
        exec_raise_type: type[Exception] | None = None,
    ) -> None:
        self._exec_exit_status = exec_exit_status
        self._exec_stderr = exec_stderr
        self._exec_raise_type = exec_raise_type

    def exec_remote_command(
        self,
        ssh_info: RemoteSSHInfo,
        command: str,
        timeout: float = 10.0,
    ) -> tuple[int, str]:
        self._exec_calls.append((ssh_info, command))
        if self._exec_raise_type is not None:
            raise self._exec_raise_type("simulated")
        return self._exec_exit_status, self._exec_stderr


def _setup_restart_test_server(
    tmp_path: Path,
    workspace_name: str = "mindtest-base",
    with_ssh_info: bool = True,
) -> tuple[TestClient, FileAuthStore, AgentId, _RecordingTunnelManager]:
    """Set up a desktop client wired to a recording tunnel manager for restart endpoint tests."""
    agent_id = AgentId()
    servers_map = {"system_interface": "http://127.0.0.1:8000"} if with_ssh_info else {}
    resolver = _RestartBackendResolver(
        url_by_agent_and_server={str(agent_id): servers_map},
        ssh_info=_TEST_SSH_INFO,
        workspace_name_by_agent_id={str(agent_id): workspace_name} if with_ssh_info else {},
    )
    tunnel_manager = _RecordingTunnelManager()

    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        tunnel_manager=tunnel_manager,
    )
    client = TestClient(app)
    _authenticate_client(client=client, auth_store=auth_store)
    return client, auth_store, agent_id, tunnel_manager


def test_restart_workspace_server_returns_403_when_unauthenticated(tmp_path: Path) -> None:
    """Without a session cookie the endpoint must refuse to act."""
    agent_id = AgentId()
    resolver = _RestartBackendResolver(
        url_by_agent_and_server={str(agent_id): {"system_interface": "http://x"}},
        ssh_info=_TEST_SSH_INFO,
        workspace_name_by_agent_id={str(agent_id): "name"},
    )
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        tunnel_manager=_RecordingTunnelManager(),
    )
    client = TestClient(app)

    response = client.post(f"/api/agents/{agent_id}/restart-workspace-server")

    assert response.status_code == 403


def test_restart_workspace_server_returns_500_for_local_without_work_dir(tmp_path: Path) -> None:
    """If the resolver exposes a workspace_name but not a work_dir, we can't find services.toml."""
    agent_id = AgentId()
    resolver = _RestartBackendResolver(
        url_by_agent_and_server={str(agent_id): {"system_interface": "http://127.0.0.1:8000"}},
        ssh_info=None,
        workspace_name_by_agent_id={str(agent_id): "local-mind"},
        work_dir_by_agent_id={},
    )
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        tunnel_manager=_RecordingTunnelManager(),
    )
    client = TestClient(app)
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.post(f"/api/agents/{agent_id}/restart-workspace-server")

    assert response.status_code == 500
    assert "work_dir" in response.text


def test_restart_workspace_server_local_happy_path_touches_services_toml(tmp_path: Path) -> None:
    """For a local agent with a known work_dir, restart runs locally and touches services.toml."""
    agent_id = AgentId()
    work_dir = tmp_path / "agent-workdir"
    work_dir.mkdir()
    resolver = _RestartBackendResolver(
        url_by_agent_and_server={str(agent_id): {"system_interface": "http://127.0.0.1:8000"}},
        ssh_info=None,
        workspace_name_by_agent_id={str(agent_id): "local-mind"},
        work_dir_by_agent_id={str(agent_id): work_dir},
    )
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        tunnel_manager=_RecordingTunnelManager(),
    )
    client = TestClient(app)
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.post(f"/api/agents/{agent_id}/restart-workspace-server")

    assert response.status_code == 200
    assert response.json()["status"] == "restarting"
    # The touch command creates services.toml if it doesn't already exist.
    # The tmux kill-window is gated by `|| true` so it's fine that no such
    # session exists in the test environment.
    assert (work_dir / "services.toml").exists()


def test_restart_workspace_server_returns_404_when_workspace_name_missing(tmp_path: Path) -> None:
    """If the resolver can't map the agent to a workspace label, we can't build the tmux target."""
    agent_id = AgentId()
    # Note: workspace_name_by_agent_id is empty -- the endpoint should not
    # be able to build a tmux target without it.
    resolver = _RestartBackendResolver(
        url_by_agent_and_server={str(agent_id): {"system_interface": "http://127.0.0.1:8000"}},
        ssh_info=_TEST_SSH_INFO,
        workspace_name_by_agent_id={},
    )
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        tunnel_manager=_RecordingTunnelManager(),
    )
    client = TestClient(app)
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.post(f"/api/agents/{agent_id}/restart-workspace-server")

    assert response.status_code == 404


def test_restart_workspace_server_happy_path_returns_200_and_issues_command(tmp_path: Path) -> None:
    """Authenticated restart of a remote agent must return 200 and SSH-exec the kill+touch combo."""
    client, _, agent_id, tunnel_manager = _setup_restart_test_server(tmp_path, workspace_name="my-mind")

    response = client.post(f"/api/agents/{agent_id}/restart-workspace-server")

    assert response.status_code == 200
    body = response.json()
    assert body["agent_id"] == str(agent_id)
    assert body["status"] == "restarting"
    assert len(tunnel_manager._exec_calls) == 1
    ssh_info_used, command_used = tunnel_manager._exec_calls[0]
    assert ssh_info_used == _TEST_SSH_INFO
    # The command must kill the bootstrap-managed svc-system_interface window
    # for the container's tmux session, and touch services.toml so bootstrap
    # reconciles it back.
    assert "minds-my-mind:svc-system_interface" in command_used
    assert "/code/services.toml" in command_used


def test_restart_workspace_server_returns_502_on_ssh_failure(tmp_path: Path) -> None:
    """Paramiko transport failures should surface as 502 so the frontend knows retrying might help."""
    client, _, agent_id, tunnel_manager = _setup_restart_test_server(tmp_path)
    tunnel_manager.configure(exec_raise_type=SSHTunnelError)

    response = client.post(f"/api/agents/{agent_id}/restart-workspace-server")

    assert response.status_code == 502


def test_restart_workspace_server_returns_500_on_nonzero_exit(tmp_path: Path) -> None:
    """A non-zero remote exit code is a server-side failure, surfaced as 500 with the stderr."""
    client, _, agent_id, tunnel_manager = _setup_restart_test_server(tmp_path)
    tunnel_manager.configure(exec_exit_status=1, exec_stderr="oh no")

    response = client.post(f"/api/agents/{agent_id}/restart-workspace-server")

    assert response.status_code == 500
    assert "oh no" in response.text


# -- Workspace server health tracker integration --


def _get_health_tracker(client: TestClient) -> WorkspaceServerHealthTracker:
    """Retrieve the WorkspaceServerHealthTracker attached to the TestClient's app state."""
    app = client.app
    assert isinstance(app, FastAPI)
    tracker = app.state.workspace_server_health_tracker
    assert isinstance(tracker, WorkspaceServerHealthTracker)
    return tracker


def test_proxy_records_failure_in_health_tracker_on_backend_disconnect(tmp_path: Path) -> None:
    """A single proxy failure shows up in the health tracker's failure log for that (agent, server)."""
    client, _, agent_id = _setup_disconnecting_backend_server(tmp_path)
    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")
    # Make the tracker fire stuck after 1 failure so we can assert it crosses
    # the threshold from a single request.
    tracker = _get_health_tracker(client)
    tracker.failure_threshold = 1

    response = client.get(
        f"/forwarding/{agent_id}/{DEFAULT_SERVER_NAME}/api/status",
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 502
    stuck = tracker.snapshot_stuck()
    assert len(stuck) == 1
    assert stuck[0].agent_id == str(agent_id)
    assert stuck[0].server_name == str(DEFAULT_SERVER_NAME)
    assert stuck[0].last_error_class == "RemoteProtocolError"


def test_proxy_records_success_clears_stuck_state(tmp_path: Path) -> None:
    """A successful proxy response must clear the stuck marker so the toast can dismiss."""
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)
    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")
    tracker = _get_health_tracker(client)
    # Seed the tracker as if it had previously marked this server stuck.
    tracker.record_failure(str(agent_id), str(DEFAULT_SERVER_NAME), "TimeoutException")
    tracker.record_failure(str(agent_id), str(DEFAULT_SERVER_NAME), "TimeoutException")
    tracker.record_failure(str(agent_id), str(DEFAULT_SERVER_NAME), "TimeoutException")
    assert len(tracker.snapshot_stuck()) == 1

    response = client.get(f"/forwarding/{agent_id}/{DEFAULT_SERVER_NAME}/api/status")

    assert response.status_code == 200
    assert tracker.snapshot_stuck() == ()


def test_snapshot_stuck_for_chrome_orders_and_serializes() -> None:
    """Chrome-side snapshot sorts entries deterministically and shapes them for SSE JSON.

    The SSE change-detection loop compares snapshots by equality, so unstable
    ordering would spuriously fire events. Cover the ordering + serialization
    shape directly since the SSE stream itself can't be driven from TestClient.
    """
    tracker = WorkspaceServerHealthTracker(failure_threshold=1)
    # Intentionally record in an order different from the expected sorted output.
    tracker.record_failure("agent-b", "system_interface", "TimeoutException")
    tracker.record_failure("agent-a", "web", "ReadError")
    tracker.record_failure("agent-a", "system_interface", "ConnectError")

    snapshot = _snapshot_stuck_for_chrome(tracker)

    # Sorted by (agent_id, server_name).
    keys = [(entry["agent_id"], entry["server_name"]) for entry in snapshot]
    assert keys == [
        ("agent-a", "system_interface"),
        ("agent-a", "web"),
        ("agent-b", "system_interface"),
    ]
    # Each entry carries exactly the fields the frontend expects.
    expected_fields = {
        "agent_id",
        "server_name",
        "stuck_since",
        "failure_count",
        "last_error_class",
        "last_error_time",
    }
    for entry in snapshot:
        assert set(entry.keys()) == expected_fields
