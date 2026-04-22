import json
import queue
import threading
from typing import Any
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel


def _summary_agents(message: dict[str, Any]) -> str:
    return f"n_agents={len(message.get('agents', []))}"


def _summary_applications(message: dict[str, Any]) -> str:
    names = [a.get("name", "?") for a in message.get("applications", [])]
    return f"apps={names}"


def _summary_proto_created(message: dict[str, Any]) -> str:
    return f"agent_id={message.get('agent_id', '?')}"


def _summary_proto_completed(message: dict[str, Any]) -> str:
    return f"agent_id={message.get('agent_id', '?')} success={message.get('success')}"


# One-liner summary builders for every WS message type. The dispatch-by-type
# pattern keeps the telemetry readable without an if/elif chain.
_BROADCAST_DETAIL_BUILDERS: Final[dict[str, Any]] = {
    "agents_updated": _summary_agents,
    "applications_updated": _summary_applications,
    "proto_agent_created": _summary_proto_created,
    "proto_agent_completed": _summary_proto_completed,
}


def _summarize_broadcast(message: dict[str, Any], target_count: int) -> str:
    msg_type = message.get("type", "<unknown>")
    build_detail = _BROADCAST_DETAIL_BUILDERS.get(msg_type)
    detail = build_detail(message) if build_detail is not None else ""
    base = f"type={msg_type} targets={target_count}"
    return f"{base} {detail}" if detail else base


class WebSocketBroadcaster(MutableModel):
    """Manages WebSocket clients and broadcasts state updates.

    Thread-safe: background threads call broadcast methods which put messages
    into per-client queues. WebSocket handlers (async) drain these queues.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _client_queues: list[queue.Queue[str | None]] = PrivateAttr(default_factory=list)

    def register(self) -> queue.Queue[str | None]:
        """Register a new WebSocket client. Returns a queue to drain for messages."""
        q: queue.Queue[str | None] = queue.Queue(maxsize=1000)
        with self._lock:
            self._client_queues.append(q)
        return q

    def unregister(self, client_queue: queue.Queue[str | None]) -> None:
        """Remove a WebSocket client's queue."""
        with self._lock:
            try:
                self._client_queues.remove(client_queue)
            except ValueError:
                pass

    def broadcast(self, message: dict[str, Any]) -> None:
        """Serialize and send a message to all connected clients. Thread-safe."""
        text = json.dumps(message)
        with self._lock:
            # Diagnostic logging for the "missing items in cloudflare dropdown"
            # bug: record the shape of every broadcast so we can correlate
            # with what the client receives (or doesn't).
            _loguru_logger.info(
                "[ws.broadcast] {}",
                _summarize_broadcast(message, target_count=len(self._client_queues)),
            )
            for q in self._client_queues:
                try:
                    q.put_nowait(text)
                except queue.Full:
                    _loguru_logger.warning("WebSocket client queue full, dropping message")

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

    def shutdown(self) -> None:
        """Signal all clients to disconnect by sending None sentinel."""
        with self._lock:
            for q in self._client_queues:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
            self._client_queues.clear()
