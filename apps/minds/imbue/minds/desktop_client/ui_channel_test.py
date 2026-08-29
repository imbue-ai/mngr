"""Channel tests: broadcaster unit behavior + the full /ui/ws path over real cheroot.

The integration tests run the actual desktop-client Flask app on the actual
WebSocket-aware cheroot server and connect with a real WebSocket client --
this is the crystallized form of the flask-sock-under-cheroot spike, so the
gateway's teardown behavior (the part that crashed whole servers when done
naively) stays pinned by tests.
"""

import json
import queue
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from simple_websocket import Client as WebSocketClient
from simple_websocket import ConnectionClosed

from imbue.minds.desktop_client.app import _ConnectedFocusedWorkspaceAgentIdsReader
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.state import DesktopClientState
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_channel import UiChannelBroadcaster
from imbue.minds.desktop_client.ui_models import UI_SCHEMA_VERSION
from imbue.minds.desktop_client.ui_models import UiClientStateMessage
from imbue.minds.desktop_client.ui_models import UiReloadMessage
from imbue.minds.desktop_client.ws_gateway import create_websocket_aware_wsgi_server

# -- Broadcaster unit tests --


def test_broadcaster_delivers_frames_to_every_registered_queue() -> None:
    broadcaster = UiChannelBroadcaster()
    first = broadcaster.register()
    second = broadcaster.register()

    broadcaster.broadcast("frame-1")

    assert first.get_nowait() == "frame-1"
    assert second.get_nowait() == "frame-1"


def test_broadcaster_unregister_stops_delivery_to_that_queue() -> None:
    broadcaster = UiChannelBroadcaster()
    first = broadcaster.register()
    broadcaster.unregister(first)

    broadcaster.broadcast("frame-after-unregister")

    assert first.empty()


def test_broadcaster_evicts_a_client_on_the_first_dropped_frame() -> None:
    """Overflow must evict immediately: the publisher's diffing never resends
    a dropped frame, so the only coherent recovery is a reconnect-resync."""
    broadcaster = UiChannelBroadcaster()
    stuck = broadcaster.register()
    healthy = broadcaster.register()
    # Fill the stuck client's queue to capacity, then broadcast once more.
    is_full = False
    while not is_full:
        try:
            stuck.put_nowait("filler")
        except queue.Full:
            is_full = True
    broadcaster.broadcast("overflow")

    # Only the overflowing client is evicted; the healthy one still gets frames.
    assert broadcaster.connection_count() == 1
    assert healthy.get_nowait() == "overflow"
    # The eviction drained the queue and left exactly the sentinel.
    drained: list[str | None] = []
    is_drained = False
    while not is_drained:
        try:
            drained.append(stuck.get_nowait())
        except queue.Empty:
            is_drained = True
    assert drained[-1] is None


def test_broadcaster_shutdown_pushes_sentinel_and_rejects_future_registrations_with_sentinel() -> None:
    broadcaster = UiChannelBroadcaster()
    before = broadcaster.register()

    broadcaster.shutdown()

    assert before.get_nowait() is None
    late = broadcaster.register()
    assert late.get_nowait() is None


def test_broadcaster_records_client_state_only_for_registered_queues() -> None:
    broadcaster = UiChannelBroadcaster()
    registered = broadcaster.register()
    unregistered: queue.Queue[str | None] = queue.Queue()
    state = UiClientStateMessage(client_id="win-1", route="/", workspace_agent_id=None)

    broadcaster.set_client_state(registered, state)
    broadcaster.set_client_state(unregistered, state)

    assert broadcaster.get_connected_client_states() == [state]


def test_connected_focused_workspace_agent_ids_excludes_unfocused_windows() -> None:
    # The notification feed's OS-dispatch gate: a workspace displayed in an
    # unfocused window (alt-tabbed away, behind another app) must not count as
    # "on screen" for OS-dispatch purposes, even though it does for the
    # in-app toast's own (focus-agnostic) on-screen check.
    broadcaster = UiChannelBroadcaster()
    focused_queue = broadcaster.register()
    unfocused_queue = broadcaster.register()
    broadcaster.set_client_state(
        focused_queue,
        UiClientStateMessage(client_id="win-focused", route="/", workspace_agent_id="agent-focused", has_focus=True),
    )
    broadcaster.set_client_state(
        unfocused_queue,
        UiClientStateMessage(
            client_id="win-unfocused", route="/", workspace_agent_id="agent-unfocused", has_focus=False
        ),
    )
    reader = _ConnectedFocusedWorkspaceAgentIdsReader(broadcaster=broadcaster)

    assert reader() == ("agent-focused",)


# -- Full-path integration over real cheroot --


@contextmanager
def _serve_ws_capable_app(tmp_path: Path) -> Iterator[tuple[int, str, DesktopClientState]]:
    """The real desktop-client app on the real WebSocket-aware cheroot server."""
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    resolver = MngrCliBackendResolver()
    app = create_desktop_client(auth_store=auth_store, backend_resolver=resolver, http_client=None)
    server = create_websocket_aware_wsgi_server(("127.0.0.1", 0), app, numthreads=6)
    server.prepare()
    port = server.bind_addr[1]
    thread = threading.Thread(target=server.serve, name="ui-channel-test-wsgi", daemon=True)
    thread.start()
    cookie = create_session_cookie(signing_key=auth_store.get_signing_key())
    try:
        yield port, cookie, get_state(app)
    finally:
        get_state(app).shutdown_event.set()
        get_state(app).ui_channel_broadcaster.shutdown()
        resolver.notify_change()
        server.stop()
        thread.join(timeout=5)


def _connect_ws(port: int, cookie: str) -> WebSocketClient:
    return WebSocketClient.connect(
        f"ws://127.0.0.1:{port}/ui/ws",
        headers={"Cookie": f"{SESSION_COOKIE_NAME}={cookie}"},
    )


def test_unauthenticated_ws_upgrade_gets_plain_401_and_server_keeps_serving(tmp_path: Path) -> None:
    with _serve_ws_capable_app(tmp_path) as (port, _cookie, _state):
        response = httpx.get(
            f"http://127.0.0.1:{port}/ui/ws",
            headers={
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Version": "13",
                "Sec-WebSocket-Key": "x3JJHMbDL1EzLkh9GBhXDw==",
            },
            timeout=5.0,
        )
        assert response.status_code == 401
        # The socket was never hijacked: the server still serves normal HTTP.
        # /login is the unauthenticated liveness probe now that every hub
        # route 302s unauthenticated navigations to it.
        followup = httpx.get(f"http://127.0.0.1:{port}/login", timeout=5.0)
        assert followup.status_code == 200


def test_ws_connect_receives_hello_then_full_snapshot(tmp_path: Path) -> None:
    # Read exactly as many frames as the sequence is expected to carry, so a
    # frame added to it fails here rather than silently going unlooked-at (and
    # taking the frame after it, in whatever test reads one, with it).
    expected_types = [
        "workspaces",
        "accounts",
        "providers",
        "requests",
        "discovery_health",
        "notifications",
        "workspace_updates",
        "environment",
    ]
    with _serve_ws_capable_app(tmp_path) as (port, cookie, _state):
        client = _connect_ws(port, cookie)
        try:
            frames = [json.loads(client.receive(timeout=5)) for _ in range(len(expected_types) + 1)]
        finally:
            client.close()
        assert frames[0]["type"] == "hello"
        assert frames[0]["schema_version"] == UI_SCHEMA_VERSION
        assert [frame["type"] for frame in frames[1:]] == expected_types


def test_broadcast_after_connect_reaches_the_ws_client(tmp_path: Path) -> None:
    with _serve_ws_capable_app(tmp_path) as (port, cookie, state):
        client = _connect_ws(port, cookie)
        try:
            assert state.ui_publisher is not None
            # However long the connect sequence is: what is under test here is
            # the push that follows it, not the sequence itself.
            for _ in state.ui_publisher.build_snapshot_frames():
                client.receive(timeout=5)
            state.ui_publisher.publish_one_shot(UiReloadMessage())
            pushed = json.loads(client.receive(timeout=5))
        finally:
            client.close()
        assert pushed == {"type": "reload_ui"}


def test_client_state_report_is_recorded_server_side(tmp_path: Path) -> None:
    with _serve_ws_capable_app(tmp_path) as (port, cookie, state):
        client = _connect_ws(port, cookie)
        try:
            client.send(
                UiClientStateMessage(client_id="win-a", route="/settings", workspace_agent_id=None).model_dump_json()
            )
            assert state.ui_channel_broadcaster.wait_for_client_ids(["win-a"], timeout_seconds=5.0)
        finally:
            client.close()


def test_client_disconnect_leaves_server_healthy_and_releases_the_connection(tmp_path: Path) -> None:
    """The spike's crash scenario: a naive integration died with EBADF on the first client close."""
    with _serve_ws_capable_app(tmp_path) as (port, cookie, state):
        client = _connect_ws(port, cookie)
        client.receive(timeout=5)
        client.close()

        assert state.ui_channel_broadcaster.wait_for_connection_count(0, timeout_seconds=5.0)
        # The server survived the close and still serves HTTP on a fresh connection.
        response = httpx.get(f"http://127.0.0.1:{port}/login", timeout=5.0)
        assert response.status_code == 200


def test_broadcaster_shutdown_evicts_live_connection_promptly(tmp_path: Path) -> None:
    """Ordered shutdown: the sentinel frees the WS worker thread without waiting out cheroot timeouts."""
    with _serve_ws_capable_app(tmp_path) as (port, cookie, state):
        client = _connect_ws(port, cookie)
        client.receive(timeout=5)

        started_at = time.monotonic()
        state.ui_channel_broadcaster.shutdown()
        # Observe the HANDLER side, not just the broadcaster's bookkeeping
        # (shutdown() clears its queue list synchronously, so asserting on
        # wait_for_connection_count alone can never fail): the sentinel must
        # make the handler thread close the peer socket well under cheroot's
        # 5s shutdown timeout (the naive path measured 5.5s in the spike).
        is_closed_by_server = False
        try:
            while time.monotonic() - started_at < 3.0:
                # A None return means this short receive timed out with the
                # socket still open; keep waiting for the server-side close,
                # which surfaces as ConnectionClosed (or OSError).
                client.receive(timeout=0.25)
        except (ConnectionClosed, OSError):
            is_closed_by_server = True
        assert is_closed_by_server
        assert time.monotonic() - started_at < 3.0
        try:
            client.close()
        except (ConnectionClosed, OSError):
            # The server already closed the connection; nothing left to close.
            pass
