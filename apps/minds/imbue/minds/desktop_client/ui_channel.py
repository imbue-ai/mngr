"""The `/ui/ws` channel: per-client fan-out plus the WebSocket route handler.

One WebSocket per SPA window replaces the legacy ``/_chrome/events`` SSE
stream. The fan-out shape is the bounded per-client-queue pattern proven in
the workspace-side system_interface (re-implemented here per the no-code-
sharing rule): background producers enqueue serialized frames per client,
each connection's handler thread drains its own queue, and a client that
stops draining is evicted with a sentinel rather than wedging a producer.

The route handler deliberately does NOT use flask-sock's ``Sock.route``
decorator: that wrapper completes the WebSocket handshake before the view
body runs, which would hijack the socket before any auth check. Here the
session-cookie check runs first and an unauthenticated upgrade gets a plain
401 on the still-intact HTTP connection; only then does simple_websocket
accept the handshake (and the handler marks the request hijacked so the
cheroot gateway suppresses its normal response path, see ``ws_gateway.py``).
"""

import queue
import threading
from collections.abc import Callable
from collections.abc import Sequence

from flask import Response
from flask import request
from loguru import logger
from pydantic import PrivateAttr
from pydantic import ValidationError
from simple_websocket import ConnectionClosed
from simple_websocket import ConnectionError as SimpleWebsocketConnectionError
from simple_websocket import Server as SimpleWebsocketServer

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.ui_models import UI_CLIENT_MESSAGE_ADAPTER
from imbue.minds.desktop_client.ui_models import UiClientStateMessage
from imbue.minds.desktop_client.ws_gateway import mark_websocket_handled

# Per-client buffer depth. State-change frames are sub-Hz in practice, so this
# represents minutes of a client falling behind before it overflows and is
# evicted. Overflow MUST evict rather than drop: the publisher diffs against
# the last frame it broadcast, so a dropped frame would never be resent and
# the client would render stale state indefinitely. Eviction closes the
# socket, and the client's reconnect-is-resync path replays a full snapshot.
_CLIENT_QUEUE_MAX_SIZE: int = 1000

# Keepalive ping cadence on each connection; detects half-dead peers without
# any asyncio machinery (each connection owns its handler thread).
_WS_PING_INTERVAL_SECONDS: int = 25

# How long the writer loop blocks on its queue per wait; bounds how quickly a
# handler observes broadcaster shutdown even if the sentinel push raced.
_WRITER_QUEUE_POLL_SECONDS: float = 30.0


def _drain_queue(client_queue: "queue.Queue[str | None]") -> None:
    is_drained = False
    while not is_drained:
        try:
            client_queue.get_nowait()
        except queue.Empty:
            is_drained = True


class UiChannelBroadcaster(MutableModel):
    """Thread-safe fan-out of serialized channel frames to every connected SPA window.

    Producers (the publisher, request handlers) call :meth:`broadcast` with an
    already-serialized frame; each connection's handler thread drains its own
    bounded queue. ``None`` on a queue is the shutdown/eviction sentinel.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    # A Condition rather than a bare Lock: connection-state changes notify
    # waiters, so shutdown paths and tests can block on real primitives
    # instead of sleep-polling. All internal state is guarded by it.
    _condition: threading.Condition = PrivateAttr(default_factory=threading.Condition)
    _client_queues: list["queue.Queue[str | None]"] = PrivateAttr(default_factory=list)
    # Latest client_state registration per connection, keyed by id(queue).
    _client_state_by_queue_id: dict[int, UiClientStateMessage] = PrivateAttr(default_factory=dict)
    _is_shut_down: bool = PrivateAttr(default=False)

    def register(self) -> "queue.Queue[str | None]":
        """Register a new connection; returns the queue its handler thread must drain."""
        client_queue: queue.Queue[str | None] = queue.Queue(maxsize=_CLIENT_QUEUE_MAX_SIZE)
        with self._condition:
            self._client_queues.append(client_queue)
            if self._is_shut_down:
                # Registration raced shutdown: hand the handler its sentinel
                # immediately so it exits instead of blocking forever.
                client_queue.put_nowait(None)
            self._condition.notify_all()
        return client_queue

    def unregister(self, client_queue: "queue.Queue[str | None]") -> None:
        """Remove a connection's queue. No-op if already removed."""
        with self._condition:
            self._client_state_by_queue_id.pop(id(client_queue), None)
            try:
                self._client_queues.remove(client_queue)
            except ValueError:
                pass
            self._condition.notify_all()

    def set_client_state(self, client_queue: "queue.Queue[str | None]", state: UiClientStateMessage) -> None:
        """Record (or update) one connection's self-reported identity/state."""
        with self._condition:
            if client_queue not in self._client_queues:
                return
            self._client_state_by_queue_id[id(client_queue)] = state
            self._condition.notify_all()

    def wait_for_connection_count(self, expected_count: int, timeout_seconds: float) -> bool:
        """Block until exactly ``expected_count`` connections are registered (or timeout)."""
        with self._condition:
            return self._condition.wait_for(
                lambda: len(self._client_queues) == expected_count, timeout=timeout_seconds
            )

    def wait_for_client_ids(self, expected_client_ids: Sequence[str], timeout_seconds: float) -> bool:
        """Block until the registered client_state ids equal ``expected_client_ids`` in order (or timeout)."""
        expected = list(expected_client_ids)
        with self._condition:
            return self._condition.wait_for(
                lambda: [state.client_id for state in self._client_state_by_queue_id.values()] == expected,
                timeout=timeout_seconds,
            )

    def get_connected_client_states(self) -> list[UiClientStateMessage]:
        """Snapshot of every registered connection's latest client_state report."""
        with self._condition:
            return list(self._client_state_by_queue_id.values())

    def connection_count(self) -> int:
        with self._condition:
            return len(self._client_queues)

    def broadcast(self, frame: str) -> None:
        """Enqueue an already-serialized frame onto every connection's queue. Thread-safe.

        A connection whose queue is full is evicted immediately: a frame it
        cannot take would otherwise be silently dropped, and the publisher's
        diffing means dropped state is never resent. Eviction closes the
        socket, so the client reconnects and resyncs from a fresh snapshot.
        """
        with self._condition:
            dead_queues: list[queue.Queue[str | None]] = []
            for client_queue in self._client_queues:
                try:
                    client_queue.put_nowait(frame)
                except queue.Full:
                    dead_queues.append(client_queue)
            for dead_queue in dead_queues:
                self._evict_locked(dead_queue)

    def _evict_locked(self, dead_queue: "queue.Queue[str | None]") -> None:
        """Evict one unresponsive connection. Caller must hold ``self._condition``."""
        self._client_state_by_queue_id.pop(id(dead_queue), None)
        try:
            self._client_queues.remove(dead_queue)
        except ValueError:
            pass
        _drain_queue(dead_queue)
        try:
            dead_queue.put_nowait(None)
        except queue.Full:
            pass
        self._condition.notify_all()
        logger.warning(
            "Evicted an unresponsive /ui/ws client: its queue overflowed at {} frames, "
            "so it must resync via reconnect rather than silently missing state",
            _CLIENT_QUEUE_MAX_SIZE,
        )

    def shutdown(self) -> None:
        """Push the sentinel to every connection so handler threads exit promptly.

        Called from the signal path BEFORE stopping the WSGI server: a live
        WebSocket otherwise pins its worker thread for cheroot's full
        ``shutdown_timeout``. Idempotent; connections arriving after shutdown
        get their sentinel at registration.
        """
        with self._condition:
            self._is_shut_down = True
            for client_queue in self._client_queues:
                _drain_queue(client_queue)
                try:
                    client_queue.put_nowait(None)
                except queue.Full:
                    pass
            self._client_queues.clear()
            self._client_state_by_queue_id.clear()
            self._condition.notify_all()


@pure
def _parse_client_frame(raw: str | bytes) -> UiClientStateMessage | None:
    """Parse one inbound client frame; None for frames that are not valid client messages."""
    try:
        text = raw.decode() if isinstance(raw, bytes) else raw
        return UI_CLIENT_MESSAGE_ADAPTER.validate_json(text)
    except (ValidationError, UnicodeDecodeError):
        return None


def _read_client_frames_until_closed(
    websocket: SimpleWebsocketServer,
    broadcaster: UiChannelBroadcaster,
    client_queue: "queue.Queue[str | None]",
) -> None:
    """Reader loop: record client_state reports until the peer closes, then wake the writer."""
    try:
        is_peer_connected = True
        while is_peer_connected:
            raw = websocket.receive()
            if raw is None:
                is_peer_connected = False
                continue
            parsed = _parse_client_frame(raw)
            if parsed is None:
                logger.debug("Ignoring an unparseable /ui/ws client frame")
                continue
            broadcaster.set_client_state(client_queue, parsed)
    except (ConnectionClosed, SimpleWebsocketConnectionError, OSError) as e:
        logger.trace("/ui/ws reader loop ended: {}", e)
    finally:
        # Wake the writer loop so the handler thread exits with the connection.
        try:
            client_queue.put_nowait(None)
        except queue.Full:
            _drain_queue(client_queue)
            try:
                client_queue.put_nowait(None)
            except queue.Full:
                pass


def run_ui_websocket_connection(
    broadcaster: UiChannelBroadcaster,
    # Called AFTER registration to serialize the connect sequence (hello
    # first); the frames are sent before any broadcast frame.
    build_snapshot_frames: Callable[[], list[str]],
) -> Response:
    """Serve one authenticated `/ui/ws` connection on the current request's socket.

    Handshake -> register with the broadcaster -> derive + send the snapshot ->
    stream broadcast frames until the client disconnects, the broadcaster
    evicts us, or shutdown. The caller has already verified authentication.
    """
    websocket = SimpleWebsocketServer.accept(request.environ, ping_interval=_WS_PING_INTERVAL_SECONDS)
    mark_websocket_handled(request.environ)
    # Register BEFORE deriving/sending the snapshot so no broadcast that fires
    # mid-snapshot is lost: a publish racing the derive lands on this queue,
    # and since queued frames are full-state messages, applying them after the
    # snapshot is always correct. (Deriving before registration would let a
    # concurrent publish update the publisher's diff state without ever
    # reaching this connection.)
    client_queue = broadcaster.register()
    reader = threading.Thread(
        target=_read_client_frames_until_closed,
        args=(websocket, broadcaster, client_queue),
        name="ui-ws-reader",
        daemon=True,
    )
    reader.start()
    try:
        for frame in build_snapshot_frames():
            websocket.send(frame)
        is_streaming = True
        while is_streaming:
            try:
                item = client_queue.get(timeout=_WRITER_QUEUE_POLL_SECONDS)
            except queue.Empty:
                # Idle poll tick: nothing to send; keepalive pings ride on
                # simple_websocket's own timer thread.
                continue
            if item is None:
                is_streaming = False
                continue
            websocket.send(item)
    except (ConnectionClosed, SimpleWebsocketConnectionError, OSError) as e:
        logger.trace("/ui/ws writer loop ended: {}", e)
    finally:
        broadcaster.unregister(client_queue)
        try:
            websocket.close()
        except (ConnectionClosed, SimpleWebsocketConnectionError, OSError):
            pass
    # Never actually written to the socket: the gateway suppresses the
    # response for hijacked requests. Present so Flask's contract holds.
    return Response(status=204)
