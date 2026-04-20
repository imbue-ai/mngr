import queue
import threading
from collections import defaultdict
from typing import Any

from imbue.minds_workspace_server.events import BufferBehavior


class AgentEventQueues:
    """Thread-safe registry of per-agent event queues.

    Adapted from llm-webchat's ConversationEventQueues but keyed by agent_id
    instead of conversation_id.
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[queue.Queue[dict[str, Any] | None]]] = defaultdict(list)
        self._event_buffers: dict[str, list[dict[str, Any]]] = {}
        # Reentrant because a CPython GC cycle during a put_nowait call inside
        # the locked register() section can finalize an abandoned SSE
        # event_generator (from an unrelated prior stream), whose `finally`
        # block calls unregister() on the same thread. The class never calls
        # its own API directly -- the runtime effectively inserts the
        # unregister() call mid-register() via a GC finalizer. With a
        # non-reentrant Lock that indirect re-entrance self-deadlocks.
        #
        # TODO: the proper fix is to either (a) move register()'s put_nowait
        # loop outside the critical section via two-phase registration
        # (needs identity tracking to survive BufferBehavior.FLUSH so the
        # ordering invariant "buffered events first, then live broadcasts"
        # is preserved), or (b) make the SSE stream handlers async def and
        # back event_queue with asyncio.Queue fed via loop.call_soon_threadsafe
        # from the watcher thread. RLock buys time but keeps
        # allocations-under-lock as a latent smell: future same-thread
        # finalizers reaching back into this API will silently succeed
        # instead of deadlocking loudly, which can mask real bugs.
        self._lock: threading.RLock = threading.RLock()
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
