"""Tests for the agent event queues."""

from imbue.claude_web_chat.event_queues import AgentEventQueues
from imbue.claude_web_chat.events import BufferBehavior


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
