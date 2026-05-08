"""Tests for the FastAPI server."""

import json
import queue
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from imbue.minds_workspace_server.activity_state import ActivityState
from imbue.minds_workspace_server.agent_discovery import AgentInfo
from imbue.minds_workspace_server.agent_manager import AgentManager
from imbue.minds_workspace_server.config import Config
from imbue.minds_workspace_server.models import AgentStateItem
from imbue.minds_workspace_server.server import create_application
from imbue.minds_workspace_server.ws_broadcaster import WebSocketBroadcaster

# Placeholder client-side port used by the refresh-service broadcast tests.
# Only the host portion of the TestClient ``client`` tuple is inspected by the
# endpoint (it enforces loopback), so any fixed value works here.
_TEST_CLIENT_PORT = 12345


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def app(config: Config) -> FastAPI:
    return create_application(config)


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


def test_index_returns_html_when_static_exists(client: TestClient, tmp_path: Path) -> None:
    """When the static dir has index.html, the server serves it."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>test</body></html>")

    with patch("imbue.minds_workspace_server.server.STATIC_DIRECTORY", static_dir):
        test_app = create_application()
        test_client = TestClient(test_app)
        response = test_client.get("/")
        assert response.status_code == 200
        assert "test" in response.text


def test_index_returns_not_built_when_no_static(client: TestClient, tmp_path: Path) -> None:
    """When static dir has no index.html, show a helpful message."""
    empty_dir = tmp_path / "static"
    empty_dir.mkdir()

    with patch("imbue.minds_workspace_server.server.STATIC_DIRECTORY", empty_dir):
        test_app = create_application()
        test_client = TestClient(test_app)
        response = test_client.get("/")
        assert response.status_code == 200
        assert "npm run build" in response.text


def test_list_agents_endpoint(client: TestClient) -> None:
    """The agents endpoint returns agent data."""
    with patch("imbue.minds_workspace_server.server.discover_agents") as mock_discover:
        mock_discover.return_value = [
            AgentInfo(
                id="agent-123",
                name="test-agent",
                state="RUNNING",
                agent_state_dir=Path("/tmp/test"),
                claude_config_dir=Path("/tmp/.claude"),
            )
        ]
        response = client.get("/api/agents")

    assert response.status_code == 200
    data = response.json()
    assert len(data["agents"]) == 1
    assert data["agents"][0]["name"] == "test-agent"
    assert data["agents"][0]["state"] == "RUNNING"


def test_get_events_for_unknown_agent(client: TestClient) -> None:
    """Getting events for a nonexistent agent returns 404."""
    with patch("imbue.minds_workspace_server.server.discover_agents", return_value=[]):
        response = client.get("/api/agents/nonexistent/events")
    assert response.status_code == 404


def test_send_message_for_unknown_agent(client: TestClient) -> None:
    """Sending a message to a nonexistent agent returns 404."""
    with patch("imbue.minds_workspace_server.server.discover_agents", return_value=[]):
        response = client.post("/api/agents/nonexistent/message", json={"message": "hello"})
    assert response.status_code == 404


def test_get_events_with_session_files(client: TestClient, tmp_path: Path) -> None:
    """Getting events for an agent with session files returns parsed events."""
    # Set up agent state dir with session history
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir(parents=True)

    # Create a session file
    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects" / "hash123"
    projects_dir.mkdir(parents=True)

    session_id = "test-session-id"
    session_file = projects_dir / f"{session_id}.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "uuid-1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "Hello"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "uuid": "uuid-2",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-6",
                    "content": [{"type": "text", "text": "Hi!"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        )
        + "\n"
    )

    # Write session history
    (agent_state_dir / "claude_session_id_history").write_text(f"{session_id}\n")

    agent_info = AgentInfo(
        id="agent-123",
        name="test-agent",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
    )
    with patch("imbue.minds_workspace_server.server._find_agent", return_value=agent_info):
        response = client.get("/api/agents/agent-123/events")

    assert response.status_code == 200
    data = response.json()
    assert len(data["events"]) == 2
    assert data["events"][0]["type"] == "user_message"
    assert data["events"][0]["content"] == "Hello"
    assert data["events"][1]["type"] == "assistant_message"
    assert data["events"][1]["text"] == "Hi!"


def test_send_message_success(client: TestClient) -> None:
    """Sending a message to a known agent succeeds."""
    agent_info = AgentInfo(
        id="agent-123",
        name="test-agent",
        state="RUNNING",
        agent_state_dir=Path("/tmp/test"),
        claude_config_dir=Path("/tmp/.claude"),
    )
    with (
        patch("imbue.minds_workspace_server.server._find_agent", return_value=agent_info),
        patch("imbue.minds_workspace_server.server.send_message", return_value=True) as mock_send,
    ):
        response = client.post("/api/agents/agent-123/message", json={"message": "hello"})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    mock_send.assert_called_once_with("test-agent", "hello")


def test_get_layout_returns_404_when_no_layout_saved(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Getting layout returns 404 when no layout file exists."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    response = client.get("/api/layout")

    assert response.status_code == 404


def test_save_and_get_layout(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Saving and retrieving a layout round-trips correctly."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    layout_data = {"dockview": {"panels": {}}, "panelParams": {"chat-1": {"panelType": "chat"}}}

    save_response = client.post("/api/layout", json=layout_data)
    assert save_response.status_code == 200
    assert save_response.json()["status"] == "ok"

    get_response = client.get("/api/layout")
    assert get_response.status_code == 200
    assert get_response.json() == layout_data


def test_save_layout_creates_directory(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Saving a layout creates the workspace_layout directory if needed."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    client.post("/api/layout", json={"test": True})

    assert (tmp_path / "agents" / "agent-123" / "workspace_layout" / "layout.json").exists()


def test_save_layout_rejects_invalid_json(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Saving invalid JSON returns 400."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    response = client.post(
        "/api/layout",
        content=b"not valid json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400


def test_index_injects_hostname_meta_tag(tmp_path: Path) -> None:
    """The index page includes a hostname meta tag."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><head></head><body>test</body></html>")

    with patch("imbue.minds_workspace_server.server.STATIC_DIRECTORY", static_dir):
        test_app = create_application()
        test_client = TestClient(test_app)
        response = test_client.get("/")
        assert response.status_code == 200
        assert "minds-workspace-server-hostname" in response.text


def test_random_name_endpoint(client: TestClient) -> None:
    """The random name endpoint returns a non-empty name."""
    response = client.get("/api/random-name")
    assert response.status_code == 200
    data = response.json()
    assert "name" in data
    assert len(data["name"]) > 0


def test_create_chat_agent_without_work_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Creating a chat agent without a primary agent work dir returns 400."""
    monkeypatch.delenv("MNGR_AGENT_WORK_DIR", raising=False)
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    test_app = create_application()
    with TestClient(test_app) as test_client:
        response = test_client.post(
            "/api/agents/create-chat",
            json={"name": "test-chat"},
        )
    assert response.status_code == 400


def test_create_worktree_agent_missing_agent(client: TestClient) -> None:
    """Creating a worktree agent with an unknown selected agent returns 400."""
    response = client.post(
        "/api/agents/create-worktree",
        json={"name": "test-worktree", "selected_agent_id": "nonexistent"},
    )
    assert response.status_code == 400


@pytest.mark.timeout(10)
def test_websocket_endpoint_sends_initial_snapshot(client: TestClient) -> None:
    """The WebSocket endpoint sends agents_updated and applications_updated on connect."""
    with client.websocket_connect("/api/ws") as ws:
        msg1 = json.loads(ws.receive_text())
        msg2 = json.loads(ws.receive_text())

        types = {msg1["type"], msg2["type"]}
        assert "agents_updated" in types
        assert "applications_updated" in types


def test_refresh_service_request_writes_event(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/refresh-service/{service_name} appends a refresh event to the agent state dir."""
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))

    response = client.post("/api/refresh-service/web")
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    events_file = tmp_path / "events" / "refresh" / "events.jsonl"
    assert events_file.exists()
    event = json.loads(events_file.read_text().splitlines()[0])
    assert event["type"] == "refresh_service"
    assert event["service_name"] == "web"


def test_refresh_service_request_without_agent_state_dir(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The request endpoint surfaces the config error when MNGR_AGENT_STATE_DIR is unset."""
    monkeypatch.delenv("MNGR_AGENT_STATE_DIR", raising=False)
    response = client.post("/api/refresh-service/web")
    assert response.status_code == 500


@pytest.mark.timeout(10)
def test_refresh_service_broadcast_emits_ws_message(app: FastAPI) -> None:
    """POST /api/refresh-service/{service_name}/broadcast sends a refresh_service WS message."""
    with TestClient(app, client=("127.0.0.1", _TEST_CLIENT_PORT)) as loopback_client:
        with loopback_client.websocket_connect("/api/ws") as ws:
            # Drain the initial snapshot messages.
            json.loads(ws.receive_text())
            json.loads(ws.receive_text())

            response = loopback_client.post("/api/refresh-service/web/broadcast")
            assert response.status_code == 200

            msg = json.loads(ws.receive_text())
            assert msg == {"type": "refresh_service", "service_name": "web"}


def test_refresh_service_broadcast_rejects_non_loopback(app: FastAPI) -> None:
    """The broadcast endpoint refuses requests whose client host isn't loopback."""
    with TestClient(app, client=("10.0.0.1", _TEST_CLIENT_PORT)) as remote_client:
        response = remote_client.post("/api/refresh-service/web/broadcast")
    assert response.status_code == 403


def test_get_events_seeds_pending_tool_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hitting /api/agents/{id}/events for a Claude session with an unmatched tool_use
    seeds the AgentManager's transcript-derived signals so the activity indicator
    reads ``TOOL_RUNNING`` immediately.
    """
    agent_id = "agent-pending-tool"
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", agent_id)
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path / "work"))

    state_dir = tmp_path / "agents" / agent_id
    state_dir.mkdir(parents=True)

    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects" / "hash123"
    projects_dir.mkdir(parents=True)
    session_id = "test-session-id"
    session_file = projects_dir / f"{session_id}.jsonl"
    # An assistant message that includes a tool_use, with no matching tool_result.
    session_file.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "uuid-1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-6",
                    "content": [
                        {"type": "text", "text": "running a command"},
                        {"type": "tool_use", "id": "call_a", "name": "Bash", "input": {"command": "ls"}},
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        )
        + "\n"
    )
    (state_dir / "claude_session_id_history").write_text(f"{session_id}\n")

    broadcaster = WebSocketBroadcaster()
    manager = AgentManager.build(broadcaster)
    with manager._lock:
        manager._agents[agent_id] = AgentStateItem(
            id=agent_id,
            name="seed-agent",
            state="RUNNING",
            labels={},
            work_dir=str(tmp_path / "work"),
        )
    manager._ensure_marker_watcher(agent_id)

    app = create_application(agent_manager=manager)
    agent_info = AgentInfo(
        id=agent_id,
        name="seed-agent",
        state="RUNNING",
        agent_state_dir=state_dir,
        claude_config_dir=claude_config_dir,
    )

    try:
        with TestClient(app) as test_client:
            with patch("imbue.minds_workspace_server.server._find_agent", return_value=agent_info):
                response = test_client.get(f"/api/agents/{agent_id}/events")
            assert response.status_code == 200

        # The watcher creation path seeds transcript-derived state
        # synchronously. Assert before ``stop()``, which clears these
        # caches alongside the marker watchers.
        with manager._lock:
            assert manager._has_unmatched_tool_use_by_agent[agent_id] is True
            assert manager._activity_state_by_agent[agent_id] == ActivityState.TOOL_RUNNING
    finally:
        manager.stop()


@pytest.mark.timeout(5)
def test_proto_agent_logs_endpoint_not_found_sends_error_and_closes(client: TestClient) -> None:
    """When the proto-agent is missing, the endpoint sends a structured not-found message and closes."""
    with client.websocket_connect("/api/proto-agents/missing-agent/logs") as ws:
        payload = json.loads(ws.receive_text())
    assert payload == {"done": True, "success": False, "error": "Proto-agent not found"}


@pytest.mark.timeout(5)
def test_proto_agent_logs_endpoint_streams_messages_until_sentinel(app: FastAPI) -> None:
    """The endpoint forwards real log lines and closes when the queue yields ``None``."""
    log_queue: queue.Queue[str | None] = queue.Queue()
    log_queue.put(json.dumps({"line": "starting"}))
    log_queue.put(json.dumps({"line": "still going"}))
    log_queue.put(None)

    with TestClient(app) as test_client:
        # The TestClient context manager triggers the lifespan startup that
        # populates ``app.state.agent_manager``; inject the queue afterwards.
        agent_manager: AgentManager = app.state.agent_manager
        agent_manager._log_queues["proto-1"] = log_queue

        with test_client.websocket_connect("/api/proto-agents/proto-1/logs") as ws:
            first = json.loads(ws.receive_text())
            second = json.loads(ws.receive_text())

    assert first == {"line": "starting"}
    assert second == {"line": "still going"}


def test_request_event_endpoint_writes_latchkey_permission_event(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/permissions/request appends a request event with server-filled metadata."""
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-7")

    response = client.post(
        "/api/permissions/request",
        json={
            "request_type": "LATCHKEY_PERMISSION",
            "service_name": "slack",
            "rationale": "to post status updates",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["event_id"].startswith("evt-")

    events_file = tmp_path / "events" / "requests" / "events.jsonl"
    assert events_file.exists()
    event = json.loads(events_file.read_text().splitlines()[0])
    assert event["event_id"] == body["event_id"]
    assert event["type"] == "latchkey_permission_request"
    assert event["source"] == "requests"
    assert event["agent_id"] == "agent-7"
    assert event["request_type"] == "LATCHKEY_PERMISSION"
    assert event["is_user_requested"] is True
    assert event["service_name"] == "slack"
    assert event["rationale"] == "to post status updates"


def test_request_event_endpoint_rejects_missing_request_type(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-7")

    response = client.post("/api/permissions/request", json={"service_name": "slack"})
    assert response.status_code == 400
    assert "request_type" in response.json()["detail"]


def test_request_event_endpoint_rejects_non_object_body(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-7")

    response = client.post("/api/permissions/request", json=["not", "an", "object"])
    assert response.status_code == 400


def test_request_event_endpoint_rejects_unknown_request_type(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-7")

    response = client.post(
        "/api/permissions/request",
        json={"request_type": "CUSTOM_THING", "service_name": "slack"},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "CUSTOM_THING" in detail
    assert "LATCHKEY_PERMISSION" in detail

    events_file = tmp_path / "events" / "requests" / "events.jsonl"
    assert not events_file.exists()


def test_request_event_endpoint_honors_caller_is_user_requested(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-7")

    response = client.post(
        "/api/permissions/request",
        json={
            "request_type": "LATCHKEY_PERMISSION",
            "service_name": "github",
            "rationale": "open PRs",
            "is_user_requested": False,
        },
    )
    assert response.status_code == 200

    events_file = tmp_path / "events" / "requests" / "events.jsonl"
    event = json.loads(events_file.read_text().splitlines()[0])
    assert event["is_user_requested"] is False


def test_request_event_endpoint_returns_500_when_agent_id_unset(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)

    response = client.post(
        "/api/permissions/request",
        json={"request_type": "LATCHKEY_PERMISSION", "service_name": "slack", "rationale": "r"},
    )
    assert response.status_code == 500
    assert "MNGR_AGENT_ID" in response.json()["detail"]
