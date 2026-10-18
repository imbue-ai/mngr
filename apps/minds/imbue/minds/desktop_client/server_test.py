"""Tests for the graceful WSGI server lifecycle (server.py).

Covers the behaviors that matter for a clean serve/shutdown cycle:
- ``desktop_client_runtime`` creates the shared HTTP client on entry and, on
  exit, sets the shutdown flag and closes the client (the ordered teardown).
- The real cheroot server keeps HTTP/1.1 connections alive across requests
  (the Electron shell's startup depends on keep-alive; see the test below).
"""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.api_socket import create_local_socket_server
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.mock_provider_relay_test import RecordingSignInCallbackForwarder
from imbue.minds.desktop_client.provider_relay import ProviderSignInRelay
from imbue.minds.desktop_client.provider_relay import RelayArmResult
from imbue.minds.desktop_client.provider_relay import SignInCallback
from imbue.minds.desktop_client.provider_relay import SignInCallbackResult
from imbue.minds.desktop_client.provider_relay import SignInOutcome
from imbue.minds.desktop_client.server import _shutdown_desktop_client
from imbue.minds.desktop_client.server import desktop_client_runtime
from imbue.minds.desktop_client.state import DesktopClientState
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.testing import serve_wsgi_server_in_thread
from imbue.minds.desktop_client.ws_gateway import create_websocket_aware_wsgi_server
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.testing import find_free_port
from imbue.mngr_latchkey.forward_supervisor import LatchkeyForwardSupervisor


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


def test_shutdown_triggers_root_concurrency_group_so_watcher_strands_exit(tmp_path: Path) -> None:
    """The teardown must trigger the group's shutdown event before exiting it.

    Mirrors the long-lived watcher strands (system-interface-health-probe,
    discovery-health-watchdog): a loop that only exits once
    ``is_shutting_down()`` flips. Without the trigger in
    ``_shutdown_desktop_client``, the group exit waits out its full exit
    timeout and abandons the strand -- observed live as a fixed 10s stall on
    every desktop-client shutdown.
    """
    concurrency_group = ConcurrencyGroup(
        name=f"test-shutdown-{uuid4().hex}",
        exit_timeout_seconds=5.0,
        shutdown_timeout_seconds=5.0,
    )
    concurrency_group.__enter__()
    is_strand_finished = threading.Event()

    def _watcher_loop() -> None:
        while not concurrency_group.is_shutting_down():
            concurrency_group.shutdown_event.wait(timeout=30.0)
        is_strand_finished.set()

    concurrency_group.start_new_thread(target=_watcher_loop, name=f"watcher-{uuid4().hex}")
    state = DesktopClientState(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=MngrCliBackendResolver(),
        root_concurrency_group=concurrency_group,
    )

    teardown_start = time.monotonic()
    _shutdown_desktop_client(state, is_externally_managed_client=True)
    teardown_elapsed_seconds = time.monotonic() - teardown_start

    # The strand must have been woken and finished (not abandoned by a timeout),
    # and the teardown must complete far below the group's 5s exit timeout.
    assert is_strand_finished.is_set()
    assert teardown_elapsed_seconds < 4.0


def test_shutdown_gives_up_every_armed_sign_in_relays_port(tmp_path: Path) -> None:
    state = DesktopClientState(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=MngrCliBackendResolver(),
    )
    relay = ProviderSignInRelay(
        workspace_id=AgentId("agent-" + "5b" * 16),
        flow_id="7c1e0f4a9d2b4e6f8a3c5d7e9f1a2b3c",
        callback=SignInCallback(port=find_free_port(), path="/callback", state="state-2d9e"),
        forwarder=RecordingSignInCallbackForwarder(
            result=SignInCallbackResult(outcome=SignInOutcome.SIGNED_IN, provider_name="Anthropic")
        ),
        on_callback_handled=lambda: None,
        no_callback_timeout_seconds=30.0,
        after_callback_seconds=30.0,
    )
    assert state.provider_relay_registry.arm(relay) is RelayArmResult.ARMED

    _shutdown_desktop_client(state, is_externally_managed_client=True)

    assert relay.is_stopped()


class _RecordingForwardSupervisor(LatchkeyForwardSupervisor):
    """A supervisor whose ``stop`` only records that it ran, instead of reaping a forward."""

    _stop_called: threading.Event = PrivateAttr(default_factory=threading.Event)

    def stop(self) -> None:
        self._stop_called.set()

    @property
    def stop_called(self) -> threading.Event:
        return self._stop_called


class _HangingForwardSupervisor(LatchkeyForwardSupervisor):
    """A supervisor whose ``stop`` blocks until released, like a forward slow to exit."""

    _release: threading.Event = PrivateAttr(default_factory=threading.Event)

    def stop(self) -> None:
        self._release.wait(timeout=30.0)

    @property
    def release(self) -> threading.Event:
        return self._release


def test_shutdown_stops_the_latchkey_forward_supervisor(tmp_path: Path) -> None:
    supervisor = _RecordingForwardSupervisor(latchkey_directory=tmp_path / "latchkey")
    state = DesktopClientState(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=MngrCliBackendResolver(),
        latchkey_forward_supervisor=supervisor,
    )

    _shutdown_desktop_client(state, is_externally_managed_client=True)

    assert supervisor.stop_called.is_set()


def test_shutdown_does_not_wait_out_a_slow_latchkey_forward_stop(tmp_path: Path) -> None:
    """The forward's stop can outlast the shell's SIGKILL grace, so the teardown must not block on it."""
    supervisor = _HangingForwardSupervisor(latchkey_directory=tmp_path / "latchkey")
    state = DesktopClientState(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=MngrCliBackendResolver(),
        latchkey_forward_supervisor=supervisor,
    )
    try:
        _shutdown_desktop_client(state, is_externally_managed_client=True)
        # The teardown returned while the stop was still in progress.
        assert not supervisor.release.is_set()
    finally:
        supervisor.release.set()


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


@contextmanager
def _serve_in_background(
    tmp_path: Path, api_socket_path: Path | None = None
) -> Iterator[tuple[int, FileAuthStore, DesktopClientState]]:
    """Run the real cheroot WSGI server on an ephemeral port (and on ``api_socket_path``) for the block."""
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    resolver = MngrCliBackendResolver()
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        minds_api_key=uuid4().hex,
    )
    # The production server classes (WebSocket-aware gateway subclass, and the
    # peer-checked local-socket listener), so these socket-level tests exercise
    # what serve_desktop_client actually runs.
    server = create_websocket_aware_wsgi_server(("127.0.0.1", 0), app, numthreads=10)
    socket_listener = (
        serve_wsgi_server_in_thread(
            create_local_socket_server(api_socket_path, app, numthreads=4), "server-test-socket"
        )
        if api_socket_path is not None
        else nullcontext()
    )
    with serve_wsgi_server_in_thread(server, "server-test-wsgi"), socket_listener:
        try:
            yield server.bind_addr[1], auth_store, get_state(app)
        finally:
            # Mirror the real shutdown: flip the flag AND fire the resolver change
            # event the SSE generators block on, so they return promptly instead of
            # making ``server.stop()`` wait out a worker wedged in its poll wait.
            get_state(app).shutdown_event.set()
            resolver.notify_change()


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
    with _serve_in_background(tmp_path) as (port, _auth_store, _state):
        # A single client connection-pool: if the server sent ``Connection:
        # close`` the second request would open a new socket.
        # Probe the static login page WITHOUT a session (an authenticated
        # /login would 307 away): it is always a direct 200 with no built SPA
        # bundle required, so this stays a pure keep-alive assertion
        # independent of whether the frontend has been built.
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            first = client.get(f"http://127.0.0.1:{port}/login")
            assert first.status_code == 200
            assert first.http_version == "HTTP/1.1"
            # The response must not force the connection closed.
            assert first.headers.get("connection", "").lower() != "close"
            second = client.get(f"http://127.0.0.1:{port}/login")
            assert second.status_code == 200
            assert second.headers.get("connection", "").lower() != "close"


def test_the_same_request_is_served_over_the_socket_and_refused_over_tcp_without_a_credential(
    tmp_path: Path, short_tmp_path: Path
) -> None:
    """The two listeners serve one app: the socket admits the user, TCP still demands a credential."""
    socket_path = short_tmp_path / "api.sock"
    with _serve_in_background(tmp_path, api_socket_path=socket_path) as (port, _auth_store, _state):
        with httpx.Client(timeout=10.0) as tcp_client:
            assert tcp_client.get(f"http://127.0.0.1:{port}/api/v1/app/version").status_code == 401
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket_path)), timeout=10.0) as socket_client:
            response = socket_client.get("http://localhost/api/v1/app/version")
        assert response.status_code == 200
        assert "workspace_template_ref" in response.json()
