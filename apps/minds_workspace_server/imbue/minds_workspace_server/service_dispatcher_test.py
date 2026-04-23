"""Integration tests for /service/<name>/ forwarding inside the workspace server.

Spins up a small stub FastAPI app on an ephemeral port as the "backend"
service, registers it with the workspace server's AgentManager via a
controlled applications.toml, and exercises the proxy end-to-end.
"""

import socket
import threading
import time
from collections.abc import AsyncGenerator
from collections.abc import Generator

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import PlainTextResponse
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket
from starlette.websockets import WebSocketDisconnect

from imbue.minds_workspace_server.agent_manager import AgentManager
from imbue.minds_workspace_server.config import Config
from imbue.minds_workspace_server.models import ApplicationEntry
from imbue.minds_workspace_server.server import create_application
from imbue.minds_workspace_server.ws_broadcaster import WebSocketBroadcaster


def _find_free_port() -> int:
    """Return an ephemeral TCP port that is currently free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _UvicornThread(threading.Thread):
    """Run a uvicorn server in a background thread for test scoping."""

    def __init__(self, app: FastAPI, port: int) -> None:
        super().__init__(daemon=True)
        self._config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="error")
        self.server = uvicorn.Server(self._config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


def _wait_for_port(port: int, timeout_seconds: float = 3.0) -> None:
    """Poll until a TCP port is accepting connections."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"Backend port {port} did not come up within {timeout_seconds}s")


def _build_stub_backend() -> FastAPI:
    """Build a tiny FastAPI app that exercises the proxy's HTML/cookie/SSE paths."""
    stub = FastAPI()

    @stub.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(
            '<html><head><title>stub</title></head><body><a href="/relative-link">rel</a></body></html>'
        )

    @stub.get("/plain")
    def plain() -> PlainTextResponse:
        return PlainTextResponse("hello")

    @stub.get("/setcookie")
    def setcookie() -> PlainTextResponse:
        response = PlainTextResponse("ok")
        response.headers["Set-Cookie"] = "sid=abc; Path=/"
        return response

    @stub.get("/json")
    def json_endpoint() -> JSONResponse:
        return JSONResponse({"ok": True})

    @stub.get("/echo-query")
    def echo_query(request: Request) -> JSONResponse:
        return JSONResponse({"query": request.url.query})

    @stub.get("/events")
    def sse_endpoint() -> StreamingResponse:
        async def gen() -> AsyncGenerator[bytes, None]:
            yield b"data: chunk-1\n\n"
            yield b"data: chunk-2\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @stub.websocket("/ws-echo")
    async def ws_echo(websocket: WebSocket) -> None:
        await websocket.accept()
        connected = True
        try:
            while connected:
                msg = await websocket.receive_text()
                await websocket.send_text(f"echo:{msg}")
        except WebSocketDisconnect:
            connected = False

    return stub


@pytest.fixture
def stub_backend() -> Generator[tuple[str, int], None, None]:
    """Start the stub backend and yield (base_url, port)."""
    port = _find_free_port()
    thread = _UvicornThread(_build_stub_backend(), port)
    thread.start()
    try:
        _wait_for_port(port)
        yield f"http://127.0.0.1:{port}", port
    finally:
        thread.stop()
        thread.join(timeout=2)


@pytest.fixture
def workspace_app_with_stub(stub_backend: tuple[str, int], monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Build a workspace_server FastAPI app wired to a stub backend under service 'web'.

    Injects a pre-built ``AgentManager`` seeded with the stub's URL as the
    'web' service. The real ``mngr observe`` pipeline is not started, so the
    test doesn't need a live mngr host; service discovery is whatever we
    put in ``_applications``.
    """
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-test")

    broadcaster = WebSocketBroadcaster()
    agent_manager = AgentManager.build(broadcaster)
    agent_manager._applications = [ApplicationEntry(name="web", url=stub_backend[0])]

    return create_application(Config(), agent_manager=agent_manager)


@pytest.fixture
def workspace_client(workspace_app_with_stub: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(workspace_app_with_stub) as client:
        yield client


def test_service_sw_js_is_served_without_stub(workspace_client: TestClient) -> None:
    """The scoped service worker is served statically from the workspace_server."""
    response = workspace_client.get("/service/web/__sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert "const PREFIX = '/service/web'" in response.text


def test_first_navigation_returns_bootstrap_when_sw_cookie_missing(workspace_client: TestClient) -> None:
    """First HTML navigation without the sw_installed cookie gets the bootstrap page."""
    response = workspace_client.get(
        "/service/web/",
        headers={"sec-fetch-mode": "navigate"},
    )
    assert response.status_code == 200
    assert "serviceWorker.register" in response.text


def test_forwarded_html_has_base_tag_and_ws_shim(workspace_client: TestClient) -> None:
    """Once the SW cookie is present, HTML responses from the backend get rewritten."""
    response = workspace_client.get(
        "/service/web/",
        headers={"sec-fetch-mode": "navigate"},
        cookies={"sw_installed_web": "1"},
    )
    assert response.status_code == 200
    assert '<base href="/service/web/">' in response.text
    assert "OrigWebSocket" in response.text


def test_forwarded_absolute_href_is_rewritten(workspace_client: TestClient) -> None:
    """Absolute-path attributes in HTML are rewritten to the service prefix."""
    response = workspace_client.get(
        "/service/web/",
        cookies={"sw_installed_web": "1"},
    )
    assert 'href="/service/web/relative-link"' in response.text


def test_forwarded_plain_text_is_unchanged(workspace_client: TestClient) -> None:
    """Non-HTML responses pass through as-is."""
    response = workspace_client.get(
        "/service/web/plain",
        cookies={"sw_installed_web": "1"},
    )
    assert response.status_code == 200
    assert response.text == "hello"


def test_forwarded_json_is_unchanged(workspace_client: TestClient) -> None:
    """JSON responses pass through as-is."""
    response = workspace_client.get(
        "/service/web/json",
        cookies={"sw_installed_web": "1"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_set_cookie_is_rewritten_to_service_path(workspace_client: TestClient) -> None:
    """Set-Cookie headers are scoped under /service/<name>/ so services don't pollute the origin."""
    response = workspace_client.get(
        "/service/web/setcookie",
        cookies={"sw_installed_web": "1"},
    )
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert "Path=/service/web/" in set_cookie


def test_unknown_service_returns_loading_page_for_html(workspace_client: TestClient) -> None:
    """Unknown service with HTML accept gets the auto-retrying loading page."""
    response = workspace_client.get(
        "/service/nonexistent/",
        headers={"accept": "text/html"},
    )
    assert response.status_code == 200
    assert "Loading..." in response.text
    assert "location.reload" in response.text


def test_unknown_service_returns_502_for_non_html(workspace_client: TestClient) -> None:
    """Unknown service for a non-HTML request returns 502 immediately."""
    response = workspace_client.get(
        "/service/nonexistent/api",
        headers={"accept": "application/json"},
    )
    assert response.status_code == 502


def test_forwarded_query_string_reaches_backend(workspace_client: TestClient) -> None:
    """Query string on the incoming request is preserved in the backend URL."""
    response = workspace_client.get(
        "/service/web/echo-query?foo=bar&baz=qux",
        cookies={"sw_installed_web": "1"},
    )
    assert response.status_code == 200
    assert response.json()["query"] == "foo=bar&baz=qux"


def test_forwarded_sse_is_streamed(workspace_client: TestClient) -> None:
    """An SSE request (accept: text/event-stream) streams chunks back to the client."""
    response = workspace_client.get(
        "/service/web/events",
        headers={"accept": "text/event-stream"},
        cookies={"sw_installed_web": "1"},
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    body = response.text
    assert "chunk-1" in body
    assert "chunk-2" in body


def test_websocket_echo_forwards_bidirectionally(workspace_client: TestClient) -> None:
    """The WS dispatcher byte-forwards messages between client and backend service."""
    with workspace_client.websocket_connect("/service/web/ws-echo") as ws:
        ws.send_text("hello")
        assert ws.receive_text() == "echo:hello"
        ws.send_text("world")
        assert ws.receive_text() == "echo:world"


def test_websocket_unknown_service_closes_with_4004(workspace_client: TestClient) -> None:
    """A WS upgrade against an unregistered service gets closed with 4004."""
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with workspace_client.websocket_connect("/service/nonexistent/anything") as ws:
            ws.receive_text()
    assert excinfo.value.code == 4004
