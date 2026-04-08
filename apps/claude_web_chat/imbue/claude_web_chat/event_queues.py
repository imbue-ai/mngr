import queue
import threading
from collections import defaultdict
from typing import Any

from imbue.claude_web_chat.events import BufferBehavior


class AgentEventQueues:
    """Thread-safe registry of per-agent event queues.

    Adapted from llm-webchat's ConversationEventQueues but keyed by agent_id
    instead of conversation_id.
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[queue.Queue[dict[str, Any] | None]]] = defaultdict(list)
        self._event_buffers: dict[str, list[dict[str, Any]]] = {}
        self._lock: threading.Lock = threading.Lock()
        self._shutdown: bool = False

    @property
    def is_shutdown(self) -> bool:
        return self._shutdown

    def register(self, agent_id: str) -> queue.Queue[dict[str, Any] | None]:
        event_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        with self._lock:
            if self._shutdown:
                event_queue.put_nowait(None)
                return event_queue
            buffered_events = self._event_buffers.get(agent_id, [])
            for event in buffered_events:
                event_queue.put_nowait(event)
            self._queues[agent_id].append(event_queue)
        return event_queue

    def unregister(self, agent_id: str, event_queue: queue.Queue[dict[str, Any] | None]) -> None:
        with self._lock:
            queues = self._queues.get(agent_id)
            if queues is not None:
                try:
                    queues.remove(event_queue)
                except ValueError:
                    pass
                if not queues:
                    del self._queues[agent_id]

    def broadcast(self, agent_id: str, event: dict[str, Any]) -> None:
        behavior = BufferBehavior(event.get("buffer_behavior", BufferBehavior.STORE))
        clean_event = {key: value for key, value in event.items() if key != "buffer_behavior"}
        with self._lock:
            if behavior is BufferBehavior.STORE:
                if agent_id not in self._event_buffers:
                    self._event_buffers[agent_id] = []
                self._event_buffers[agent_id].append(clean_event)
            elif behavior is BufferBehavior.FLUSH:
                self._event_buffers.pop(agent_id, None)
            queues = list(self._queues.get(agent_id, []))
        for event_queue in queues:
            event_queue.put_nowait(clean_event)

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            for agent_queues in self._queues.values():
                for event_queue in agent_queues:
                    event_queue.put_nowait(None)
            self._queues.clear()
            self._event_buffers.clear()
