"""Tests for the WebSocket broadcaster."""

import json
import queue

from imbue.minds_workspace_server.ws_broadcaster import WebSocketBroadcaster


def _get_message(q: queue.Queue[str | None]) -> str:
    """Get a non-None message from the queue."""
    value = q.get_nowait()
    assert value is not None
    return value


def test_register_returns_queue() -> None:
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()
    assert isinstance(q, queue.Queue)


def test_broadcast_puts_message_in_all_queues() -> None:
    broadcaster = WebSocketBroadcaster()
    q1 = broadcaster.register()
    q2 = broadcaster.register()

    broadcaster.broadcast({"type": "test", "data": 42})

    msg1 = json.loads(_get_message(q1))
    msg2 = json.loads(_get_message(q2))
    assert msg1 == {"type": "test", "data": 42}
    assert msg2 == {"type": "test", "data": 42}


def test_unregister_removes_queue() -> None:
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()
    broadcaster.unregister(q)

    broadcaster.broadcast({"type": "test"})
    assert q.empty()


def test_unregister_nonexistent_is_safe() -> None:
    broadcaster = WebSocketBroadcaster()
    other_queue: queue.Queue[str | None] = queue.Queue()
    broadcaster.unregister(other_queue)


def test_broadcast_agents_updated() -> None:
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()

    agents = [{"id": "a1", "name": "agent-1", "state": "RUNNING"}]
    broadcaster.broadcast_agents_updated(agents)

    msg = json.loads(_get_message(q))
    assert msg["type"] == "agents_updated"
    assert msg["agents"] == agents


def test_broadcast_applications_updated() -> None:
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()

    apps = [{"name": "web", "url": "http://localhost:8000"}]
    broadcaster.broadcast_applications_updated(apps)

    msg = json.loads(_get_message(q))
    assert msg["type"] == "applications_updated"
    assert msg["applications"] == apps


def test_broadcast_proto_agent_created() -> None:
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()

    broadcaster.broadcast_proto_agent_created(
        agent_id="a1", name="test", creation_type="worktree", parent_agent_id=None
    )

    msg = json.loads(_get_message(q))
    assert msg["type"] == "proto_agent_created"
    assert msg["agent_id"] == "a1"
    assert msg["creation_type"] == "worktree"
    assert msg["parent_agent_id"] is None


def test_broadcast_proto_agent_completed() -> None:
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()

    broadcaster.broadcast_proto_agent_completed(agent_id="a1", success=True, error=None)

    msg = json.loads(_get_message(q))
    assert msg["type"] == "proto_agent_completed"
    assert msg["success"] is True
    assert msg["error"] is None


def test_broadcast_refresh_service() -> None:
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()

    broadcaster.broadcast_refresh_service("web")

    msg = json.loads(_get_message(q))
    assert msg == {"type": "refresh_service", "server_name": "web"}


def test_shutdown_sends_none_sentinel() -> None:
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()

    broadcaster.shutdown()

    assert q.get_nowait() is None


def test_broadcast_drops_when_queue_full() -> None:
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()

    for i in range(1001):
        broadcaster.broadcast({"index": i})

    count = 0
    while not q.empty():
        q.get_nowait()
        count += 1
    assert count == 1000
