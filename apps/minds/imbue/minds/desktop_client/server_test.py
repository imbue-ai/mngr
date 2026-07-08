"""Tests for the graceful WSGI server lifecycle (server.py).

Covers the two behaviors that matter for "shut down quickly and cleanly":
- ``desktop_client_runtime`` creates the shared HTTP client on entry and, on
  exit, sets the shutdown flag and closes the client (the ordered teardown).
- The chrome-events SSE generator observes ``shutdown_event`` and returns
  cleanly (running its ``finally`` cleanup), rather than blocking forever --
  this is what lets the server drain without cancelling a stream mid-flight.
"""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from cheroot import wsgi
from flask.testing import FlaskClient

from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.server import desktop_client_runtime
from imbue.minds.desktop_client.state import DesktopClientState
from imbue.minds.desktop_client.state import get_state


def _build_authenticated_client(tmp_path: Path) -> tuple[FlaskClient, MngrCliBackendResolver]:
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    resolver = MngrCliBackendResolver()
    app = create_desktop_client(auth_store=auth_store, backend_resolver=resolver, http_client=None)
    client = app.test_client()
    client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(signing_key=auth_store.get_signing_key()))
    return client, resolver


def test_runtime_creates_http_client_on_entry_and_closes_it_with_shutdown_flag(tmp_path: Path) -> None:
    """The runtime owns the shared HTTP client lifecycle and flips the shutdown flag on exit.

    ``root_concurrency_group`` is left None so the runtime skips the geo-detection
    strand (which would make a network call) and the concurrency-group drain;
    those are exercised by the live app, not this unit test.
    """
    state = DesktopClientState(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=MngrCliBackendResolver(),
    )
    assert state.http_client is None
    assert not state.shutdown_event.is_set()

    with desktop_client_runtime(state, is_externally_managed_client=False):
        assert state.http_client is not None
        assert not state.http_client.is_closed

    assert state.shutdown_event.is_set()
    assert state.http_client is not None
    assert state.http_client.is_closed


def test_runtime_leaves_externally_managed_http_client_untouched(tmp_path: Path) -> None:
    """An injected HTTP client (e.g. from a test) is neither replaced nor closed by the runtime."""
    injected = httpx.Client()
    state = DesktopClientState(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=MngrCliBackendResolver(),
        http_client=injected,
    )
    with desktop_client_runtime(state, is_externally_managed_client=True):
        assert state.http_client is injected

    assert not injected.is_closed
    injected.close()


def test_chrome_events_sse_returns_cleanly_when_shutting_down(tmp_path: Path) -> None:
    """The chrome SSE generator emits its initial snapshot then returns once the shutdown
    flag is set, instead of entering its blocking wait loop -- and removes its resolver
    change callback on the way out so no listener leaks.
    """
    client, resolver = _build_authenticated_client(tmp_path)
    # Pre-set the shutdown flag so the generator yields its connect-time snapshot
    # and then exits the loop immediately (a deterministic, non-blocking path).
    get_state(client.application).shutdown_event.set()

    response = client.get("/_chrome/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.get_data(as_text=True)
    # The connect-time snapshot always includes the workspaces event.
    assert '"type": "workspaces"' in body
    # The generator's finally removed its on-change callback (no leak).
    assert resolver._on_change_callbacks == []


@contextmanager
def _serve_in_background(tmp_path: Path) -> Iterator[tuple[int, FileAuthStore, DesktopClientState]]:
    """Run the real cheroot WSGI server on an ephemeral port for the duration of the block."""
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    resolver = MngrCliBackendResolver()
    app = create_desktop_client(auth_store=auth_store, backend_resolver=resolver, http_client=None)
    server = wsgi.Server(("127.0.0.1", 0), app)
    server.prepare()
    port = server.bind_addr[1]
    thread = threading.Thread(target=server.serve, name="server-test-wsgi", daemon=True)
    thread.start()
    try:
        yield port, auth_store, get_state(app)
    finally:
        # Mirror the real shutdown: flip the flag AND fire the resolver change
        # event the SSE generators block on, so they return promptly instead of
        # making ``server.stop()`` wait out a worker wedged in its poll wait.
        get_state(app).shutdown_event.set()
        resolver.notify_change()
        server.stop()
        thread.join(timeout=5)


def test_chrome_events_sse_streams_chunked_over_http_1_1(tmp_path: Path) -> None:
    """Over a real socket the SSE endpoint streams chunked HTTP/1.1 incrementally.

    A Content-Length-less streaming generator must reach an EventSource
    chunk-by-chunk, not after the whole response is buffered. cheroot speaks
    HTTP/1.1 and chunk-encodes the SSE; assert it end-to-end.
    """
    with _serve_in_background(tmp_path) as (port, auth_store, _state):
        cookie = create_session_cookie(signing_key=auth_store.get_signing_key())
        headers = {"Cookie": f"{SESSION_COOKIE_NAME}={cookie}"}
        with httpx.stream("GET", f"http://127.0.0.1:{port}/_chrome/events", headers=headers, timeout=10.0) as response:
            assert response.status_code == 200
            assert response.http_version == "HTTP/1.1"
            assert response.headers.get("transfer-encoding") == "chunked"
            assert response.headers["content-type"].startswith("text/event-stream")
            # The first streamed event arrives promptly (incrementally), not after
            # the whole response is buffered.
            deadline = time.monotonic() + 5.0
            saw_workspaces = False
            for line in response.iter_lines():
                if line.startswith("data:") and "workspaces" in line:
                    saw_workspaces = True
                    break
                if time.monotonic() > deadline:
                    break
            assert saw_workspaces


def test_server_keeps_connections_alive(tmp_path: Path) -> None:
    """The server reuses a single TCP connection across requests (HTTP/1.1 keep-alive).

    The Werkzeug development server hardcodes ``Connection: close`` on every
    response, so it never reuses a connection. The Electron shell's startup
    consumes the one-time code with a ``net.request`` to ``/authenticate`` that
    307-redirects to ``/`` and awaits the followed response; Chromium's network
    stack does not follow that redirect cleanly when the 307 closes the socket,
    hanging UI startup. Keep-alive (which cheroot provides) is what the shell was
    built against -- assert two requests share one connection and that the
    redirect target is reachable on the reused connection.
    """
    with _serve_in_background(tmp_path) as (port, auth_store, _state):
        cookie = create_session_cookie(signing_key=auth_store.get_signing_key())
        headers = {"Cookie": f"{SESSION_COOKIE_NAME}={cookie}"}
        # A single client connection-pool: if the server sent ``Connection:
        # close`` the second request would open a new socket.
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            first = client.get(f"http://127.0.0.1:{port}/", headers=headers)
            assert first.status_code == 200
            assert first.http_version == "HTTP/1.1"
            # The response must not force the connection closed.
            assert first.headers.get("connection", "").lower() != "close"
            second = client.get(f"http://127.0.0.1:{port}/welcome", headers=headers)
            assert second.status_code == 200
            assert second.headers.get("connection", "").lower() != "close"
