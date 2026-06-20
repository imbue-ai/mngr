"""Tests for the graceful WSGI server lifecycle (server.py).

Covers the two behaviors that matter for "shut down quickly and cleanly":
- ``desktop_client_runtime`` creates the shared HTTP client on entry and, on
  exit, sets the shutdown flag and closes the client (the ordered teardown).
- The chrome-events SSE generator observes ``shutdown_event`` and returns
  cleanly (running its ``finally`` cleanup), rather than blocking forever --
  this is what lets the server drain without cancelling a stream mid-flight.
"""

from pathlib import Path

import httpx
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
