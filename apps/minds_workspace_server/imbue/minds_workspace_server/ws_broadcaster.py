import asyncio
import json
import queue
import threading
from typing import Any

from loguru import logger as _loguru_logger
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel

# Per-client buffer depth. Holds at most this many state-change broadcasts before
# the broadcaster starts dropping the oldest. State-change broadcasts are
# typically sub-Hz, so 1000 messages represents well over a minute of falling
# behind even under burst load.
_CLIENT_QUEUE_MAX_SIZE = 1000

# How many *consecutive* broadcasts a single client can be ``queue.Full`` for
# before the broadcaster gives up on it. A momentarily-slow client whose handler
# drains even one message between broadcasts resets the counter and stays
# connected. Only a client that makes zero progress over this many broadcasts
# gets disconnected.
_MAX_CONSECUTIVE_QUEUE_FULL = 50


def _drain_queue(client_queue: queue.Queue[str | None]) -> None:
    """Remove all pending items from ``client_queue`` so it ends up empty."""
    is_drained = False
    while not is_drained:
        try:
            client_queue.get_nowait()
        except queue.Empty:
            is_drained = True


class WebSocketBroadcaster(MutableModel):
    """Manages WebSocket clients and broadcasts state updates.

    Thread-safe: background threads call broadcast methods which put messages
    into per-client queues. WebSocket handlers (async) drain these queues.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _client_queues: list[queue.Queue[str | None]] = PrivateAttr(default_factory=list)
    # Number of consecutive broadcasts a given client's queue has been full for.
    # Keyed by ``id(queue)`` to avoid hashing the queue itself. Reset to 0 on any
    # successful enqueue. A client is only disconnected once its counter reaches
    # ``_MAX_CONSECUTIVE_QUEUE_FULL`` -- a brief stall is tolerated.
    _consecutive_queue_full_by_id: dict[int, int] = PrivateAttr(default_factory=dict)
    # Per-client (handler_task, loop) recorded by ``register`` when called from
    # an asyncio Task, so the broadcaster can cancel a wedged WS handler from
    # its background broadcast threads. Cancellation is the mechanism that
    # frees a coroutine blocked in ``await websocket.send_text(...)`` on a
    # half-dead TCP connection: there is no per-send wall-clock timeout.
    # Tradeoff: if the handler is wedged but no broadcasts arrive, the
    # consecutive-overflow threshold never trips and the coroutine stays
    # parked. Acceptable in practice because state-change broadcasts are
    # continuous in normal operation.
    _handler_by_id: dict[int, tuple[asyncio.Task[Any], asyncio.AbstractEventLoop]] = PrivateAttr(default_factory=dict)

    def register(self) -> queue.Queue[str | None]:
        """Register a new WebSocket client. Returns a queue to drain for messages.

        When called from inside an asyncio Task, the broadcaster captures that
        task and its loop so eviction can cancel the wedged handler via
        ``loop.call_soon_threadsafe(task.cancel)``. When called from a sync
        context (no running loop), eviction simply drops the queue.
        """
        try:
            loop = asyncio.get_running_loop()
            handler_task = asyncio.current_task()
        except RuntimeError:
            loop = None
            handler_task = None
        q: queue.Queue[str | None] = queue.Queue(maxsize=_CLIENT_QUEUE_MAX_SIZE)
        with self._lock:
            self._client_queues.append(q)
            self._consecutive_queue_full_by_id[id(q)] = 0
            if handler_task is not None and loop is not None:
                self._handler_by_id[id(q)] = (handler_task, loop)
        return q

    def unregister(self, client_queue: queue.Queue[str | None]) -> None:
        """Remove a WebSocket client's queue."""
        with self._lock:
            self._consecutive_queue_full_by_id.pop(id(client_queue), None)
            self._handler_by_id.pop(id(client_queue), None)
            try:
                self._client_queues.remove(client_queue)
            except ValueError:
                pass

    def broadcast(self, message: dict[str, Any]) -> None:
        """Serialize and send a message to all connected clients. Thread-safe."""
        text = json.dumps(message)
        with self._lock:
            dead_queues: list[queue.Queue[str | None]] = []
            for q in self._client_queues:
                try:
                    q.put_nowait(text)
                    self._consecutive_queue_full_by_id[id(q)] = 0
                except queue.Full:
                    new_count = self._consecutive_queue_full_by_id.get(id(q), 0) + 1
                    self._consecutive_queue_full_by_id[id(q)] = new_count
                    if new_count >= _MAX_CONSECUTIVE_QUEUE_FULL:
                        dead_queues.append(q)
            for dead_queue in dead_queues:
                self._disconnect_locked(dead_queue)

    def _disconnect_locked(self, dead_queue: queue.Queue[str | None]) -> None:
        """Drop ``dead_queue`` and cancel its handler task. Caller must hold ``self._lock``."""
        handler = self._handler_by_id.pop(id(dead_queue), None)
        self._consecutive_queue_full_by_id.pop(id(dead_queue), None)
        try:
            self._client_queues.remove(dead_queue)
        except ValueError:
            pass
        _drain_queue(dead_queue)
        if handler is not None:
            task, loop = handler
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError as e:
                # The loop has already been closed (eg. during process
                # shutdown). The handler task is no longer reachable from
                # this thread; the WS connection will be torn down with the
                # loop. Log at debug since this is a benign termination race.
                _loguru_logger.debug("Skipped cancel of evicted WebSocket handler: loop closed ({})", e)
        _loguru_logger.warning(
            "Disconnected unresponsive WebSocket client after {} consecutive queue-full broadcasts",
            _MAX_CONSECUTIVE_QUEUE_FULL,
        )

    def broadcast_agents_updated(self, agents: list[dict[str, Any]]) -> None:
        """Broadcast an agents_updated event."""
        self.broadcast({"type": "agents_updated", "agents": agents})

    def broadcast_applications_updated(self, applications: list[dict[str, str]]) -> None:
        """Broadcast an applications_updated event."""
        self.broadcast({"type": "applications_updated", "applications": applications})

    def broadcast_proto_agent_created(
        self,
        agent_id: str,
        name: str,
        creation_type: str,
        parent_agent_id: str | None,
    ) -> None:
        """Broadcast a proto_agent_created event."""
        self.broadcast(
            {
                "type": "proto_agent_created",
                "agent_id": agent_id,
                "name": name,
                "creation_type": creation_type,
                "parent_agent_id": parent_agent_id,
            }
        )

    def broadcast_proto_agent_completed(self, agent_id: str, success: bool, error: str | None) -> None:
        """Broadcast a proto_agent_completed event."""
        self.broadcast(
            {
                "type": "proto_agent_completed",
                "agent_id": agent_id,
                "success": success,
                "error": error,
            }
        )

    def broadcast_refresh_service(self, service_name: str) -> None:
        """Broadcast a refresh_service event telling the frontend to reload a web-service tab."""
        self.broadcast({"type": "refresh_service", "service_name": service_name})

    def shutdown(self) -> None:
        """Signal all clients to disconnect by sending None sentinel."""
        with self._lock:
            for q in self._client_queues:
                _drain_queue(q)
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
            self._client_queues.clear()
            self._consecutive_queue_full_by_id.clear()
            self._handler_by_id.clear()
