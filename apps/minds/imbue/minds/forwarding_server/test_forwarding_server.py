from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi import Request as FastAPIRequest
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from imbue.minds.config.data_types import MindPaths
from imbue.minds.forwarding_server.agent_creator import AgentCreator
from imbue.minds.forwarding_server.app import create_forwarding_server
from imbue.minds.forwarding_server.auth import FileAuthStore
from imbue.minds.forwarding_server.backend_resolver import BackendResolverInterface
from imbue.minds.forwarding_server.backend_resolver import MngCliBackendResolver
from imbue.minds.forwarding_server.backend_resolver import StaticBackendResolver
from imbue.minds.forwarding_server.conftest import DEFAULT_SERVER_NAME
from imbue.minds.forwarding_server.conftest import make_agents_json
from imbue.minds.forwarding_server.conftest import make_resolver_with_data
from imbue.minds.forwarding_server.conftest import make_server_log
from imbue.minds.forwarding_server.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.forwarding_server.ssh_tunnel import RemoteSSHInfo
from imbue.minds.forwarding_server.ssh_tunnel import SSHTunnelError
from imbue.minds.forwarding_server.ssh_tunnel import SSHTunnelManager
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import ServerName
from imbue.mng.primitives import AgentId


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


def _create_test_forwarding_server(
    tmp_path: Path,
    backend_resolver: BackendResolverInterface,
    http_client: httpx.AsyncClient | None,
    agent_creator: AgentCreator | None = None,
) -> tuple[TestClient, FileAuthStore]:
    """Create a forwarding server with the given backend resolver."""
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)

    app = create_forwarding_server(
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
    """Set up a forwarding server with a test backend for proxy testing."""
    agent_id = AgentId()

    backend_app = _create_test_backend()
    test_http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backend_app),
        base_url="http://test-backend",
    )

    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={str(agent_id): {str(server_name): "http://test-backend"}},
    )
    client, auth_store = _create_test_forwarding_server(
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


def test_landing_page_redirects_when_single_agent_known(tmp_path: Path) -> None:
    """When authenticated and exactly one agent is known, the landing page redirects to it."""
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/agents/{}/".format(agent_id)


# -- Agent default redirect tests --


def test_agent_default_page_redirects_to_web_server(tmp_path: Path) -> None:
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={
            str(agent_id): {"web": "http://test-backend:9100"},
        },
    )
    client, auth_store = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get(f"/agents/{agent_id}/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == f"/agents/{agent_id}/web/"


def test_agent_default_page_rejects_unauthenticated_requests(tmp_path: Path) -> None:
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={str(agent_id): {"web": "http://test-backend"}},
    )
    client, _ = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.get(f"/agents/{agent_id}/", follow_redirects=False)
    assert response.status_code == 403


# -- Agent servers page tests --


def test_agent_servers_page_lists_available_servers(tmp_path: Path) -> None:
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={
            str(agent_id): {"web": "http://test-backend:9100", "api": "http://test-backend:9200"},
        },
    )
    client, auth_store = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get(f"/agents/{agent_id}/servers/")
    assert response.status_code == 200
    assert "web" in response.text
    assert "api" in response.text
    assert f"/agents/{agent_id}/web/" in response.text
    assert f"/agents/{agent_id}/api/" in response.text


def test_agent_servers_page_shows_empty_state_when_no_servers(tmp_path: Path) -> None:
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={str(agent_id): {}})
    client, auth_store = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get(f"/agents/{agent_id}/servers/")
    assert response.status_code == 200
    assert "No servers are currently running" in response.text


def test_agent_servers_page_rejects_unauthenticated_requests(tmp_path: Path) -> None:
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={str(agent_id): {"web": "http://test-backend"}},
    )
    client, _ = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.get(f"/agents/{agent_id}/servers/")
    assert response.status_code == 403


# -- Proxy tests (now with server_name in URL) --


def test_agent_proxy_rejects_unauthenticated_requests(tmp_path: Path) -> None:
    client, _, agent_id = _setup_test_server(tmp_path)

    response = client.get(f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/")
    assert response.status_code == 403


def test_agent_proxy_serves_bootstrap_on_first_navigation(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get(
        f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/",
        headers={"sec-fetch-mode": "navigate"},
    )

    assert response.status_code == 200
    assert "serviceWorker.register" in response.text


def test_agent_proxy_serves_service_worker_js(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get(f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/__sw.js")
    assert response.status_code == 200
    assert "application/javascript" in response.headers["content-type"]
    assert "skipWaiting" in response.text


def test_agent_proxy_forwards_get_request_to_backend(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)

    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")

    response = client.get(f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/api/status")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_agent_proxy_forwards_post_request_to_backend(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)

    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")

    response = client.post(
        f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/api/echo",
        content=b"test-body-content",
    )
    assert response.status_code == 200
    assert response.json() == {"echo": "test-body-content"}


def test_agent_proxy_injects_websocket_shim_into_html_responses(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)

    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")

    response = client.get(f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/")
    assert response.status_code == 200
    assert "OrigWebSocket" in response.text
    assert "Hello from backend" in response.text


def _setup_test_server_without_backend(
    tmp_path: Path,
) -> tuple[TestClient, FileAuthStore, AgentId]:
    """Set up a forwarding server with no backends for testing error paths."""
    agent_id = AgentId()

    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, auth_store = _create_test_forwarding_server(
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
        f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/",
        headers={"Accept": "text/html"},
    )
    assert response.status_code == 200
    assert "Loading..." in response.text
    assert "location.reload()" in response.text


def test_agent_proxy_returns_502_for_unknown_backend_non_html(tmp_path: Path) -> None:
    client, _, agent_id = _setup_test_server_without_backend(tmp_path)

    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")

    response = client.get(
        f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/api/status",
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
        with client.websocket_connect(f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/ws"):
            pass

    assert exc_info.value.code == 4003


def test_websocket_proxy_rejects_unknown_backend(tmp_path: Path) -> None:
    client, _, agent_id = _setup_test_server_without_backend(tmp_path)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/ws"):
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
    client, auth_store = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=test_http_client,
    )

    _authenticate_client(client=client, auth_store=auth_store)
    client.cookies.set(f"sw_installed_{agent_id}_web", "1")
    client.cookies.set(f"sw_installed_{agent_id}_api", "1")

    web_response = client.get(f"/agents/{agent_id}/web/")
    assert web_response.status_code == 200
    assert web_response.json() == {"server": "web"}

    api_response = client.get(f"/agents/{agent_id}/api/")
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

    client, auth_store = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=test_http_client,
    )

    # Not authenticated yet - both servers reject
    response_web = client.get(f"/agents/{agent_id}/web/")
    assert response_web.status_code == 403
    response_api = client.get(f"/agents/{agent_id}/api/")
    assert response_api.status_code == 403

    # Authenticate once (global session)
    _authenticate_client(client=client, auth_store=auth_store)

    client.cookies.set(f"sw_installed_{agent_id}_web", "1")
    client.cookies.set(f"sw_installed_{agent_id}_api", "1")

    # Both servers are now accessible
    response_web = client.get(f"/agents/{agent_id}/web/api/status")
    assert response_web.status_code == 200

    response_api = client.get(f"/agents/{agent_id}/api/api/status")
    assert response_api.status_code == 200


# -- Integration test: MngCliBackendResolver with forwarding server --


def test_mng_cli_resolver_proxies_to_backend_discovered_via_mng_cli(tmp_path: Path) -> None:
    """Full integration test: the MngCliBackendResolver calls mng CLI to discover
    the agent's server URL, and the forwarding server proxies HTTP requests through."""
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
    client, auth_store = _create_test_forwarding_server(
        tmp_path=data_dir,
        backend_resolver=backend_resolver,
        http_client=test_http_client,
    )

    assert backend_resolver.get_backend_url(agent_id, ServerName("web")) == "http://test-backend"
    assert agent_id in backend_resolver.list_known_agent_ids()

    _authenticate_client(client=client, auth_store=auth_store)
    client.cookies.set(f"sw_installed_{agent_id}_web", "1")

    response = client.get(f"/agents/{agent_id}/web/api/status")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    response = client.post(
        f"/agents/{agent_id}/web/api/echo",
        content=b"integration-test",
    )
    assert response.status_code == 200
    assert response.json() == {"echo": "integration-test"}


def test_mng_cli_resolver_multi_server_integration(tmp_path: Path) -> None:
    """Integration test: MngCliBackendResolver with multiple servers per agent."""
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
    client, auth_store = _create_test_forwarding_server(
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
    web_response = client.get(f"/agents/{agent_id}/web/health")
    assert web_response.status_code == 200
    assert web_response.json() == {"source": "web"}

    api_response = client.get(f"/agents/{agent_id}/api/health")
    assert api_response.status_code == 200
    assert api_response.json() == {"source": "api"}


def test_mng_cli_resolver_returns_loading_page_when_backend_unavailable(tmp_path: Path) -> None:
    """When backend is not available, the proxy returns a loading page that retries client-side."""
    agent_id = AgentId()
    data_dir = tmp_path / "minds_data"

    backend_resolver = MngCliBackendResolver()
    client, auth_store = _create_test_forwarding_server(
        tmp_path=data_dir,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    _authenticate_client(client=client, auth_store=auth_store)
    client.cookies.set(f"sw_installed_{agent_id}_web", "1")

    response = client.get(f"/agents/{agent_id}/web/", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "Loading..." in response.text
    assert "location.reload()" in response.text


def test_mng_cli_resolver_landing_page_redirects_single_discovered_agent(tmp_path: Path) -> None:
    """When a single agent is discovered and authenticated, the landing page redirects to it."""
    agent_id = AgentId()
    data_dir = tmp_path / "minds_data"

    backend_resolver = make_resolver_with_data(
        server_logs={str(agent_id): make_server_log("web", "http://test-backend")},
        agents_json=make_agents_json(agent_id),
    )
    client, auth_store = _create_test_forwarding_server(
        tmp_path=data_dir,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/agents/{}/".format(agent_id)


def test_mng_cli_resolver_agent_servers_page_via_mng_cli(tmp_path: Path) -> None:
    """The agent servers page lists servers discovered via mng events."""
    agent_id = AgentId()
    data_dir = tmp_path / "minds_data"

    log_content = make_server_log("web", "http://test:9100") + make_server_log("api", "http://test:9200")

    backend_resolver = make_resolver_with_data(
        server_logs={str(agent_id): log_content},
        agents_json=make_agents_json(agent_id),
    )
    client, auth_store = _create_test_forwarding_server(
        tmp_path=data_dir,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get(f"/agents/{agent_id}/servers/")
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
    """Set up a forwarding server with a tunnel manager that always fails."""
    agent_id = AgentId()
    backend_resolver = _RemoteStaticBackendResolver(
        url_by_agent_and_server={str(agent_id): {"web": "http://127.0.0.1:9100"}},
        ssh_info=_TEST_SSH_INFO,
    )
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)

    app = create_forwarding_server(
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

    response = client.get(f"/agents/{agent_id}/web/api/status")
    assert response.status_code == 502
    assert "SSH tunnel" in response.text


def test_websocket_proxy_closes_with_1011_when_ssh_tunnel_fails(tmp_path: Path) -> None:
    """When SSH tunnel setup fails, the WebSocket should close with code 1011."""
    client, _, agent_id = _setup_failing_tunnel_server(tmp_path)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/agents/{agent_id}/web/ws"):
            pass

    assert exc_info.value.code == 1011


def test_http_proxy_without_tunnel_manager_works_for_local_backend(tmp_path: Path) -> None:
    """When no tunnel_manager is provided, local backends work normally."""
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)
    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")

    response = client.get(f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/api/status")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
    client, auth_store = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=test_http_client,
    )
    _authenticate_client(client=client, auth_store=auth_store)
    client.cookies.set(f"sw_installed_{agent_id}_chat", "1")

    # Request with no additional query -- only stored query should arrive
    response = client.get(f"/agents/{agent_id}/chat/")
    assert response.status_code == 200
    assert response.json()["query"] == "arg=chat"

    # Request with additional query -- both should be combined
    response = client.get(f"/agents/{agent_id}/chat/", params={"arg": "CONV123"})
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
    response = client.get(f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/api/status", params={"foo": "bar"})
    assert response.status_code == 200


# -- Landing page agent creation tests --


def test_landing_page_shows_create_form_when_no_agents_exist(tmp_path: Path) -> None:
    """When authenticated and no agents exist, the landing page shows the agent creation form."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, auth_store = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/")
    assert response.status_code == 200
    assert "Create a Mind" in response.text
    assert "git_url" in response.text


def test_landing_page_prefills_git_url_from_query_param(tmp_path: Path) -> None:
    """The create form pre-fills the git URL from a query parameter."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, auth_store = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/", params={"git_url": "https://github.com/test/repo"})
    assert response.status_code == 200
    assert "https://github.com/test/repo" in response.text


def test_create_page_shows_form(tmp_path: Path) -> None:
    """GET /create shows the agent creation form."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, auth_store = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/create")
    assert response.status_code == 200
    assert "Create a Mind" in response.text


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
    client, auth_store = _create_test_forwarding_server(
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
    client, auth_store = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.post("/create", data={"git_url": "https://github.com/test/repo"})
    assert response.status_code == 501


def test_create_agent_api_returns_501_without_agent_creator(tmp_path: Path) -> None:
    """POST /api/create-agent returns 501 when no agent_creator is configured."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, auth_store = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.post("/api/create-agent", json={"git_url": "https://github.com/test/repo"})
    assert response.status_code == 501


def test_creating_page_returns_501_without_agent_creator(tmp_path: Path) -> None:
    """GET /creating/{id} returns 501 when no agent_creator is configured."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, auth_store = _create_test_forwarding_server(
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
    """Create a forwarding server with an agent creator for testing.

    The returned client is already authenticated with a global session.
    """
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    agent_creator = AgentCreator(
        paths=MindPaths(data_dir=tmp_path / "minds"),
    )
    client, auth_store = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
        agent_creator=agent_creator,
    )
    _authenticate_client(client=client, auth_store=auth_store)
    return client, auth_store, agent_creator


def test_create_form_submit_redirects_to_creating_page(tmp_path: Path) -> None:
    """POST /create with valid git_url redirects to /creating/{agent_id}."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/create",
        data={"git_url": "https://github.com/test/repo"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/creating/")


def test_create_form_submit_rejects_empty_git_url(tmp_path: Path) -> None:
    """POST /create with empty git_url returns 400."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post("/create", data={"git_url": "", "agent_name": "test"})
    assert response.status_code == 400


def test_create_form_submit_passes_agent_name(tmp_path: Path) -> None:
    """POST /create passes agent_name to the creator."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/create",
        data={"git_url": "https://github.com/test/repo", "agent_name": "my-agent"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_create_agent_api_passes_agent_name(tmp_path: Path) -> None:
    """POST /api/create-agent passes agent_name to the creator."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/api/create-agent",
        json={"git_url": "https://github.com/test/repo", "agent_name": "my-agent"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "agent_id" in data


def test_create_agent_api_returns_agent_id(tmp_path: Path) -> None:
    """POST /api/create-agent returns JSON with agent_id and status."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post("/api/create-agent", json={"git_url": "https://github.com/test/repo"})
    assert response.status_code == 200
    data = response.json()
    assert "agent_id" in data
    assert data["status"] == "CLONING"


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

    agent_id = agent_creator.start_creation("https://github.com/test/repo")

    response = client.get("/creating/{}".format(agent_id))
    assert response.status_code == 200
    assert "Creating your mind" in response.text


def test_creating_page_returns_404_for_unknown(tmp_path: Path) -> None:
    """GET /creating/{agent_id} returns 404 for unknown agent creation."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.get("/creating/{}".format(AgentId()))
    assert response.status_code == 404


def test_creation_status_api_returns_status_for_tracked_agent(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/status returns a valid status for a tracked creation."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    agent_id = agent_creator.start_creation("https://github.com/test/repo")

    response = client.get("/api/create-agent/{}/status".format(agent_id))
    assert response.status_code == 200
    data = response.json()
    assert data["agent_id"] == str(agent_id)
    assert data["status"] in ("CLONING", "CREATING", "DONE", "FAILED")


def test_create_page_prefills_git_url_from_query(tmp_path: Path) -> None:
    """GET /create?git_url=... pre-fills the form."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.get("/create", params={"git_url": "https://github.com/test/repo"})
    assert response.status_code == 200
    assert "https://github.com/test/repo" in response.text


def test_landing_page_shows_create_link_when_multiple_agents_known(tmp_path: Path) -> None:
    """When authenticated with multiple agents known, landing page shows 'Create another mind' link."""
    agent_id_1 = AgentId()
    agent_id_2 = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={
            str(agent_id_1): {"web": "http://test:9100"},
            str(agent_id_2): {"web": "http://test:9200"},
        },
    )
    client, auth_store = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/")
    assert response.status_code == 200
    assert "Create another mind" in response.text


def test_create_page_rejects_unauthenticated(tmp_path: Path) -> None:
    """GET /create returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, _ = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.get("/create")
    assert response.status_code == 403


def test_create_form_submit_rejects_unauthenticated(tmp_path: Path) -> None:
    """POST /create returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, _ = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.post("/create", data={"git_url": "https://github.com/test/repo"})
    assert response.status_code == 403


def test_create_agent_api_rejects_unauthenticated(tmp_path: Path) -> None:
    """POST /api/create-agent returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, _ = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.post("/api/create-agent", json={"git_url": "https://github.com/test/repo"})
    assert response.status_code == 403


def test_creation_status_api_rejects_unauthenticated(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/status returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, _ = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.get("/api/create-agent/{}/status".format(AgentId()))
    assert response.status_code == 403


def test_creation_logs_sse_returns_501_without_agent_creator(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/logs returns 501 when no agent_creator."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, auth_store = _create_test_forwarding_server(
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
    client, _ = _create_test_forwarding_server(
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

    agent_id = agent_creator.start_creation("https://github.com/test/repo")

    with client.stream("GET", "/api/create-agent/{}/logs".format(agent_id)) as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")


def test_creating_page_rejects_unauthenticated(tmp_path: Path) -> None:
    """GET /creating/{id} returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    client, _ = _create_test_forwarding_server(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.get("/creating/{}".format(AgentId()))
    assert response.status_code == 403


def test_create_form_submit_passes_launch_mode(tmp_path: Path) -> None:
    """POST /create passes launch_mode to the creator."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/create",
        data={
            "git_url": "https://github.com/test/repo",
            "agent_name": "my-agent",
            "launch_mode": "DEV",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_create_agent_api_passes_launch_mode(tmp_path: Path) -> None:
    """POST /api/create-agent passes launch_mode to the creator."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/api/create-agent",
        json={
            "git_url": "https://github.com/test/repo",
            "agent_name": "my-agent",
            "launch_mode": "DEV",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "agent_id" in data


def test_create_agent_api_rejects_invalid_launch_mode(tmp_path: Path) -> None:
    """POST /api/create-agent returns 400 for an invalid launch_mode."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/api/create-agent",
        json={
            "git_url": "https://github.com/test/repo",
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
