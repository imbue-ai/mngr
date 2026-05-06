import json
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi import Request as FastAPIRequest
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.app import _build_workspace_list
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import DEFAULT_SERVICE_NAME
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_service_log
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import create_sharing_request_event
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import ServiceName
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.polling import wait_for


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
    client = TestClient(app, base_url="http://localhost")

    return client, auth_store


def _setup_test_server(
    tmp_path: Path,
    service_name: ServiceName = DEFAULT_SERVICE_NAME,
) -> tuple[TestClient, FileAuthStore, AgentId]:
    """Set up a desktop client with a test backend for proxy testing."""
    agent_id = AgentId()

    backend_app = _create_test_backend()
    test_http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backend_app),
        base_url="http://test-backend",
    )

    backend_resolver = StaticBackendResolver(
        url_by_agent_and_service={str(agent_id): {str(service_name): "http://test-backend"}},
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
    """Authenticate a test client by minting a signed session cookie and adding it to the jar.

    The production path (GET /authenticate?one_time_code=...) returns a
    ``Set-Cookie`` with ``Domain=localhost`` so the cookie is valid on both
    ``localhost`` and ``<agent-id>.localhost`` subdomains. httpx's TestClient
    cookie jar is stricter than real browsers about Domain=localhost and
    silently drops that cookie on subsequent requests, so we set the cookie
    directly on the jar here instead of round-tripping through /authenticate.
    The server-side logic the test is exercising is independent of the
    Set-Cookie emission path; the bare presence/signature of the cookie is
    what ``_is_authenticated`` checks.
    """
    cookie_value = create_session_cookie(signing_key=auth_store.get_signing_key())
    # Intentionally no Domain=: httpx's cookie jar silently drops Domain=localhost
    # cookies on subsequent requests even with base_url=http://localhost.
    client.cookies.set(SESSION_COOKIE_NAME, cookie_value, path="/")


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


# -- Agent servers page tests --


# -- Proxy tests (now with service_name in URL) --


def _setup_test_server_without_backend(
    tmp_path: Path,
) -> tuple[TestClient, FileAuthStore, AgentId]:
    """Set up a desktop client with no backends for testing error paths."""
    agent_id = AgentId()

    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    _authenticate_client(client=client, auth_store=auth_store)

    return client, auth_store, agent_id


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


# -- Multi-server proxy tests --


# -- Integration test: MngrCliBackendResolver with desktop client --


def test_mngr_cli_resolver_landing_page_lists_single_discovered_agent(tmp_path: Path) -> None:
    """When a single agent is discovered and authenticated, the landing page lists it."""
    agent_id = AgentId()
    data_dir = tmp_path / "minds_data"

    backend_resolver = make_resolver_with_data(
        service_logs={str(agent_id): make_service_log("web", "http://test-backend")},
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
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
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
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
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
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
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

    # The URL handle is a ``CreationId`` (minds-internal in-flight handle),
    # not a canonical mngr ``AgentId``; passing an AgentId-prefixed string
    # would now fail to parse and never even reach the not-tracked check.
    unknown_id = CreationId()
    response = client.get("/api/create-agent/{}/status".format(unknown_id))
    assert response.status_code == 404


def test_landing_page_lists_agents_when_multiple_known(tmp_path: Path) -> None:
    """When authenticated and multiple agents are known, the landing page lists them all."""
    agent_id_1 = AgentId()
    agent_id_2 = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_service={
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
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
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
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
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
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
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

    The ``AgentCreator.root_concurrency_group`` is an ad-hoc group entered for
    the helper and left active for the caller's test duration. These tests only
    exercise HTTP endpoints (status polling, form rendering, etc.) -- they do
    not actually run agent creation subprocesses against the group, so leaving
    it in the ACTIVE state until GC is acceptable here.
    """
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    root_cg = ConcurrencyGroup(name="test-root")
    root_cg.__enter__()
    agent_creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_cg,
        notification_dispatcher=NotificationDispatcher.create(is_electron=False, tkinter_module=None, is_macos=False),
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

    response = client.get("/creating/{}".format(CreationId()))
    assert response.status_code == 404


def test_creation_status_api_returns_status_for_tracked_agent(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/status returns a valid status for a tracked creation."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    creation_id = agent_creator.start_creation("file:///nonexistent-repo")

    response = client.get("/api/create-agent/{}/status".format(creation_id))
    assert response.status_code == 200
    data = response.json()
    # The status response now reports both ``creation_id`` (always present)
    # and ``agent_id`` (only once mngr create returns a canonical id). For
    # this test the create runs against a nonexistent repo so it may never
    # produce an agent_id; just check that the creation_id round-trips.
    assert data["creation_id"] == str(creation_id)
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
        url_by_agent_and_service={
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
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, _ = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.get("/create")
    assert response.status_code == 403


def test_create_form_submit_rejects_unauthenticated(tmp_path: Path) -> None:
    """POST /create returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, _ = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.post("/create", data={"git_url": "file:///nonexistent-repo"})
    assert response.status_code == 403


def test_create_agent_api_rejects_unauthenticated(tmp_path: Path) -> None:
    """POST /api/create-agent returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, _ = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.post("/api/create-agent", json={"git_url": "file:///nonexistent-repo"})
    assert response.status_code == 403


def test_creation_status_api_rejects_unauthenticated(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/status returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, _ = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.get("/api/create-agent/{}/status".format(AgentId()))
    assert response.status_code == 403


def test_creation_logs_sse_returns_501_without_agent_creator(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/logs returns 501 when no agent_creator."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
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
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
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

    response = client.get("/api/create-agent/{}/logs".format(CreationId()))
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
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
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
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
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

    client = TestClient(app, base_url="http://localhost", raise_server_exceptions=False)
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
    # Interactivity including the SSE fallback has moved to the external JS.
    assert "/_static/sidebar.js" in response.text


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
        url_by_agent_and_service={str(agent_id): {str(DEFAULT_SERVICE_NAME): "http://test-backend"}},
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

    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        session_store=session_store,
        minds_config=minds_config,
        request_inbox=request_inbox,
        paths=WorkspacePaths(data_dir=tmp_path),
    )
    client = TestClient(app, base_url="http://localhost")
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
    event = create_sharing_request_event(agent_id=agent_id, service_name="web")
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    session_store = MultiAccountSessionStore(data_dir=tmp_path)
    minds_config = MindsConfig(data_dir=tmp_path)
    request_inbox = RequestInbox().add_request(event)
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        session_store=session_store,
        minds_config=minds_config,
        request_inbox=request_inbox,
        paths=WorkspacePaths(data_dir=tmp_path),
    )
    client = TestClient(app, base_url="http://localhost")
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


_TEST_PREAUTH_COOKIE = "test-preauth-cookie-value"
_TEST_MNGR_FORWARD_PORT = 8421


def _build_refresh_test_app(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
) -> tuple[FastAPI, list[httpx.Request]]:
    """Wire a desktop client app for refresh-event tests.

    Returns the app and a ``received`` list that captures every
    ``httpx.Request`` the app's http_client sees. The caller is
    responsible for entering the TestClient context (or deliberately
    skipping it to exercise the pre-lifespan code path).
    """
    received: list[httpx.Request] = []

    async def _capture(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200, json={"ok": True})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_capture))

    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=resolver,
        http_client=http_client,
        session_store=MultiAccountSessionStore(data_dir=tmp_path),
        minds_config=MindsConfig(data_dir=tmp_path),
        request_inbox=RequestInbox(),
        paths=WorkspacePaths(data_dir=tmp_path),
        mngr_forward_port=_TEST_MNGR_FORWARD_PORT,
        mngr_forward_preauth_cookie=_TEST_PREAUTH_COOKIE,
    )
    return app, received


def test_refresh_event_posts_to_system_interface_broadcast(tmp_path: Path) -> None:
    """A refresh event on the mngr event stream triggers a POST to the
    plugin's per-agent subdomain so the workspace server broadcasts. The URL
    is on the plugin's port and the request carries the ``mngr_forward_session``
    cookie (set to the preauth value minds wired in)."""
    agent_id = AgentId()
    service_name = "web"

    resolver = make_resolver_with_data(
        agents_json=make_agents_json(agent_id),
        service_logs={str(agent_id): make_service_log("system_interface", "http://ws-backend:9000")},
    )
    app, received = _build_refresh_test_app(tmp_path, resolver)

    with TestClient(app):
        raw_line = json.dumps({"source": "refresh", "type": "refresh_service", "service_name": service_name})
        resolver._fire_on_refresh(str(agent_id), raw_line)
        wait_for(
            lambda: len(received) > 0,
            timeout=2.0,
            poll_interval=0.02,
            error_message="refresh broadcast POST never arrived",
        )

    assert len(received) == 1, f"expected one POST, got {len(received)}: {[str(r.url) for r in received]}"
    request = received[0]
    assert request.method == "POST"
    expected_url = (
        f"http://{agent_id}.localhost:{_TEST_MNGR_FORWARD_PORT}/api/refresh-service/{service_name}/broadcast"
    )
    assert str(request.url) == expected_url
    cookie_header = request.headers.get("cookie", "")
    assert f"mngr_forward_session={_TEST_PREAUTH_COOKIE}" in cookie_header


def test_refresh_event_before_lifespan_is_dropped_without_raising(tmp_path: Path) -> None:
    """A refresh event that fires before the app's lifespan has run does not crash.

    Reproduces the startup-ordering race: in production, stream_manager.start()
    runs before uvicorn.run(app), so refresh events can arrive in the window
    between create_desktop_client (which registers the callback) and the
    lifespan startup (which captures the event loop). The callback must drop
    the event rather than raising AttributeError on app.state.event_loop.
    """
    agent_id = AgentId()

    resolver = make_resolver_with_data(
        agents_json=make_agents_json(agent_id),
        service_logs={str(agent_id): make_service_log("system_interface", "http://ws-backend:9000")},
    )
    _app, received = _build_refresh_test_app(tmp_path, resolver)

    # Deliberately do NOT enter a TestClient context -- the lifespan has never
    # fired, so app.state.event_loop is still None.
    raw_line = json.dumps({"source": "refresh", "service_name": "web"})
    resolver._fire_on_refresh(str(agent_id), raw_line)

    assert received == []
