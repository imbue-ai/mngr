from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi import Request as FastAPIRequest
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from imbue.changelings.forwarding_server.app import create_forwarding_server
from imbue.changelings.forwarding_server.auth import FileAuthStore
from imbue.changelings.forwarding_server.backend_resolver import BackendResolverInterface
from imbue.changelings.forwarding_server.backend_resolver import MngCliBackendResolver
from imbue.changelings.forwarding_server.backend_resolver import StaticBackendResolver
from imbue.changelings.forwarding_server.conftest import DEFAULT_SERVER_NAME
from imbue.changelings.forwarding_server.conftest import FakeMngCli
from imbue.changelings.forwarding_server.conftest import make_agents_json
from imbue.changelings.forwarding_server.conftest import make_server_log
from imbue.changelings.forwarding_server.cookie_manager import get_cookie_name_for_agent
from imbue.changelings.forwarding_server.ssh_tunnel import RemoteSSHInfo
from imbue.changelings.forwarding_server.ssh_tunnel import SSHTunnelError
from imbue.changelings.forwarding_server.ssh_tunnel import SSHTunnelManager
from imbue.changelings.primitives import OneTimeCode
from imbue.changelings.primitives import ServerName
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
) -> tuple[TestClient, FileAuthStore]:
    """Create a forwarding server with the given backend resolver."""
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)

    app = create_forwarding_server(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=http_client,
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
    agent_id: AgentId,
) -> None:
    """Authenticate a test client for an agent by adding a code and consuming it."""
    code = OneTimeCode(f"auth-{AgentId()}")
    auth_store.add_one_time_code(agent_id=agent_id, code=code)
    client.get(
        "/authenticate",
        params={"agent_id": str(agent_id), "one_time_code": str(code)},
        follow_redirects=False,
    )


def test_landing_page_shows_empty_state_without_cookies(tmp_path: Path) -> None:
    client, _, _ = _setup_test_server(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert "No changelings are accessible" in response.text


def test_login_redirects_to_authenticate_via_js(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    code = OneTimeCode(f"login-code-{AgentId()}")
    auth_store.add_one_time_code(agent_id=agent_id, code=code)

    response = client.get(
        "/login",
        params={"agent_id": str(agent_id), "one_time_code": str(code)},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "window.location.href" in response.text
    assert "/authenticate" in response.text


def test_authenticate_with_valid_code_sets_cookie_and_redirects(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    code = OneTimeCode(f"auth-code-{AgentId()}")
    auth_store.add_one_time_code(agent_id=agent_id, code=code)

    response = client.get(
        "/authenticate",
        params={"agent_id": str(agent_id), "one_time_code": str(code)},
        follow_redirects=False,
    )

    assert response.status_code == 307
    cookie_name = get_cookie_name_for_agent(agent_id)
    assert cookie_name in response.cookies


def test_authenticate_redirects_to_agent_servers_page(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    code = OneTimeCode(f"auth-code-{AgentId()}")
    auth_store.add_one_time_code(agent_id=agent_id, code=code)

    response = client.get(
        "/authenticate",
        params={"agent_id": str(agent_id), "one_time_code": str(code)},
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == f"/agents/{agent_id}/"


def test_authenticate_with_invalid_code_returns_403(tmp_path: Path) -> None:
    client, _, agent_id = _setup_test_server(tmp_path)

    response = client.get(
        "/authenticate",
        params={"agent_id": str(agent_id), "one_time_code": "bogus-code-82734"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "invalid or has already been used" in response.text


def test_authenticate_code_cannot_be_reused(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    code = OneTimeCode(f"once-only-{AgentId()}")
    auth_store.add_one_time_code(agent_id=agent_id, code=code)

    first_response = client.get(
        "/authenticate",
        params={"agent_id": str(agent_id), "one_time_code": str(code)},
        follow_redirects=False,
    )
    assert first_response.status_code == 307

    second_response = client.get(
        "/authenticate",
        params={"agent_id": str(agent_id), "one_time_code": str(code)},
        follow_redirects=False,
    )
    assert second_response.status_code == 403


def test_landing_page_shows_agent_after_authentication(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)

    response = client.get("/")
    assert response.status_code == 200
    assert str(agent_id) in response.text


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
    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)

    response = client.get(f"/agents/{agent_id}/")
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
    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)

    response = client.get(f"/agents/{agent_id}/")
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

    response = client.get(f"/agents/{agent_id}/")
    assert response.status_code == 403


# -- Proxy tests (now with server_name in URL) --


def test_agent_proxy_rejects_unauthenticated_requests(tmp_path: Path) -> None:
    client, _, agent_id = _setup_test_server(tmp_path)

    response = client.get(f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/")
    assert response.status_code == 403


def test_agent_proxy_serves_bootstrap_on_first_navigation(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)

    response = client.get(
        f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/",
        headers={"sec-fetch-mode": "navigate"},
    )

    assert response.status_code == 200
    assert "serviceWorker.register" in response.text


def test_agent_proxy_serves_service_worker_js(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)

    response = client.get(f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/__sw.js")
    assert response.status_code == 200
    assert "application/javascript" in response.headers["content-type"]
    assert "skipWaiting" in response.text


def test_agent_proxy_forwards_get_request_to_backend(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)

    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")

    response = client.get(f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/api/status")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_agent_proxy_forwards_post_request_to_backend(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)

    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")

    response = client.post(
        f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/api/echo",
        content=b"test-body-content",
    )
    assert response.status_code == 200
    assert response.json() == {"echo": "test-body-content"}


def test_agent_proxy_injects_websocket_shim_into_html_responses(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)

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

    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)

    return client, auth_store, agent_id


def test_agent_proxy_returns_502_for_unknown_backend(tmp_path: Path) -> None:
    client, _, agent_id = _setup_test_server_without_backend(tmp_path)

    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")

    response = client.get(f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/")
    assert response.status_code == 502


def test_login_redirects_if_already_authenticated(tmp_path: Path) -> None:
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)

    new_code = OneTimeCode(f"second-code-{AgentId()}")
    auth_store.add_one_time_code(agent_id=agent_id, code=new_code)

    response = client.get(
        "/login",
        params={"agent_id": str(agent_id), "one_time_code": str(new_code)},
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

    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)
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

    # Authenticate once (per-agent)
    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)

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
    data_dir = tmp_path / "changelings_data"

    fake_cli = FakeMngCli(
        server_logs={str(agent_id): make_server_log("web", "http://test-backend")},
        agents_json=make_agents_json(agent_id),
    )

    backend_app = _create_test_backend()
    test_http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backend_app),
        base_url="http://test-backend",
    )

    backend_resolver = MngCliBackendResolver(mng_cli=fake_cli)
    client, auth_store = _create_test_forwarding_server(
        tmp_path=data_dir,
        backend_resolver=backend_resolver,
        http_client=test_http_client,
    )

    assert backend_resolver.get_backend_url(agent_id, ServerName("web")) == "http://test-backend"
    assert agent_id in backend_resolver.list_known_agent_ids()

    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)
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
    data_dir = tmp_path / "changelings_data"

    log_content = make_server_log("web", "http://web-backend") + make_server_log("api", "http://api-backend")
    fake_cli = FakeMngCli(
        server_logs={str(agent_id): log_content},
        agents_json=make_agents_json(agent_id),
    )

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

    backend_resolver = MngCliBackendResolver(mng_cli=fake_cli)
    client, auth_store = _create_test_forwarding_server(
        tmp_path=data_dir,
        backend_resolver=backend_resolver,
        http_client=test_http_client,
    )

    # Verify resolver sees both servers
    servers = backend_resolver.list_servers_for_agent(agent_id)
    assert ServerName("web") in servers
    assert ServerName("api") in servers

    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)
    client.cookies.set(f"sw_installed_{agent_id}_web", "1")
    client.cookies.set(f"sw_installed_{agent_id}_api", "1")

    # Verify each server routes correctly
    web_response = client.get(f"/agents/{agent_id}/web/health")
    assert web_response.status_code == 200
    assert web_response.json() == {"source": "web"}

    api_response = client.get(f"/agents/{agent_id}/api/health")
    assert api_response.status_code == 200
    assert api_response.json() == {"source": "api"}


def test_mng_cli_resolver_returns_502_when_mng_logs_fails(tmp_path: Path) -> None:
    """When mng logs fails (agent has no servers.jsonl), the proxy returns 502."""
    agent_id = AgentId()
    data_dir = tmp_path / "changelings_data"

    fake_cli = FakeMngCli(server_logs={}, agents_json=None)
    backend_resolver = MngCliBackendResolver(mng_cli=fake_cli)
    client, auth_store = _create_test_forwarding_server(
        tmp_path=data_dir,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)
    client.cookies.set(f"sw_installed_{agent_id}_web", "1")

    response = client.get(f"/agents/{agent_id}/web/")
    assert response.status_code == 502


def test_mng_cli_resolver_landing_page_shows_discovered_agents(tmp_path: Path) -> None:
    """The landing page should list agents discovered via mng list."""
    agent_id = AgentId()
    data_dir = tmp_path / "changelings_data"

    fake_cli = FakeMngCli(
        server_logs={str(agent_id): make_server_log("web", "http://test-backend")},
        agents_json=make_agents_json(agent_id),
    )

    backend_resolver = MngCliBackendResolver(mng_cli=fake_cli)
    client, auth_store = _create_test_forwarding_server(
        tmp_path=data_dir,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)

    response = client.get("/")
    assert response.status_code == 200
    assert str(agent_id) in response.text


def test_mng_cli_resolver_agent_servers_page_via_mng_cli(tmp_path: Path) -> None:
    """The agent servers page lists servers discovered via mng logs."""
    agent_id = AgentId()
    data_dir = tmp_path / "changelings_data"

    log_content = make_server_log("web", "http://test:9100") + make_server_log("api", "http://test:9200")
    fake_cli = FakeMngCli(
        server_logs={str(agent_id): log_content},
        agents_json=make_agents_json(agent_id),
    )

    backend_resolver = MngCliBackendResolver(mng_cli=fake_cli)
    client, auth_store = _create_test_forwarding_server(
        tmp_path=data_dir,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)

    response = client.get(f"/agents/{agent_id}/")
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
    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)
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
    _authenticate_client(client=client, auth_store=auth_store, agent_id=agent_id)
    client.cookies.set(f"sw_installed_{agent_id}_{DEFAULT_SERVER_NAME}", "1")

    response = client.get(f"/agents/{agent_id}/{DEFAULT_SERVER_NAME}/api/status")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
