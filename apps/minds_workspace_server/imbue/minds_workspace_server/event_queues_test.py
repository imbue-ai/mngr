"""Tests for the agent event queues."""

import threading

from imbue.minds_workspace_server.event_queues import AgentEventQueues
from imbue.minds_workspace_server.events import BufferBehavior


def test_broadcast_delivers_to_registered_queue() -> None:
    queues = AgentEventQueues()
    q = queues.register("agent-1")
    queues.broadcast("agent-1", {"type": "test", "data": "hello"})
    event = q.get_nowait()
    assert event == {"type": "test", "data": "hello"}


def test_broadcast_does_not_deliver_to_other_agents() -> None:
    queues = AgentEventQueues()
    q1 = queues.register("agent-1")
    q2 = queues.register("agent-2")
    queues.broadcast("agent-1", {"type": "test"})
    assert not q2.empty() or q2.qsize() == 0
    assert q1.get_nowait() == {"type": "test"}
    assert q2.empty()


def test_unregister_removes_queue() -> None:
    queues = AgentEventQueues()
    q = queues.register("agent-1")
    queues.unregister("agent-1", q)
    queues.broadcast("agent-1", {"type": "test"})
    assert q.empty()


def test_buffer_replay_on_register() -> None:
    queues = AgentEventQueues()
    queues.broadcast("agent-1", {"type": "event-1"})
    queues.broadcast("agent-1", {"type": "event-2"})
    q = queues.register("agent-1")
    assert q.get_nowait() == {"type": "event-1"}
    assert q.get_nowait() == {"type": "event-2"}


def test_buffer_flush() -> None:
    queues = AgentEventQueues()
    queues.broadcast("agent-1", {"type": "event-1"})
    queues.broadcast("agent-1", {"type": "flush", "buffer_behavior": BufferBehavior.FLUSH})
    q = queues.register("agent-1")
    assert q.empty()


def test_buffer_ignore() -> None:
    queues = AgentEventQueues()
    queues.broadcast("agent-1", {"type": "event-1"})
    queues.broadcast("agent-1", {"type": "ephemeral", "buffer_behavior": BufferBehavior.IGNORE})
    q = queues.register("agent-1")
    assert q.get_nowait() == {"type": "event-1"}
    assert q.empty()


def test_buffer_behavior_stripped_from_delivered_events() -> None:
    queues = AgentEventQueues()
    q = queues.register("agent-1")
    queues.broadcast("agent-1", {"type": "test", "buffer_behavior": BufferBehavior.STORE})
    event = q.get_nowait()
    assert event is not None
    assert "buffer_behavior" not in event


def test_shutdown_sends_none_to_all() -> None:
    queues = AgentEventQueues()
    q1 = queues.register("agent-1")
    q2 = queues.register("agent-2")
    queues.shutdown()
    assert q1.get_nowait() is None
    assert q2.get_nowait() is None
    assert queues.is_shutdown


def test_register_after_shutdown_returns_closed_queue() -> None:
    queues = AgentEventQueues()
    queues.shutdown()
    q = queues.register("agent-1")
    assert q.get_nowait() is None


def test_register_tolerates_reentrant_unregister_from_same_thread() -> None:
    """register() runs arbitrary allocations inside its critical section
    (the put_nowait loop that replays buffered events). CPython can fire a
    GC cycle at any of those allocation points, and if GC finalizes an
    abandoned SSE event_generator the generator's `finally` block calls
    unregister() synchronously on the same thread. The registry's lock
    must be reentrant so that re-entrance does not self-deadlock.

    We simulate the re-entrance deterministically by installing a
    buffered-events list whose __iter__ calls unregister. If the lock is
    non-reentrant, register() deadlocks on itself and the wait times out.
    """
    queues = AgentEventQueues()
    existing_queue = queues.register("agent-1")

    class ReentrantOnIter(list[dict[str, object]]):
        def __iter__(self):
            queues.unregister("agent-1", existing_queue)
            return super().__iter__()

    queues._event_buffers["agent-1"] = ReentrantOnIter([{"type": "event"}])

    finished = threading.Event()

    def run_register() -> None:
        queues.register("agent-1")
        finished.set()

    worker = threading.Thread(target=run_register, daemon=True)
    worker.start()
    assert finished.wait(timeout=2.0), (
        "register() deadlocked; the lock must be reentrant so finalizers "
        "that call back into AgentEventQueues from the same thread succeed"
    )
