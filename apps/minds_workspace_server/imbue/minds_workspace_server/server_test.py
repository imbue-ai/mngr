"""Tests for the FastAPI server."""

import asyncio
import json
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from imbue.minds_workspace_server.agent_discovery import AgentInfo
from imbue.minds_workspace_server.agent_manager import AgentManager
from imbue.minds_workspace_server.config import Config
from imbue.minds_workspace_server.server import _run_ws_broadcast_loop
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


class _HangingWebSocket:
    """Test double for ``WebSocket`` whose ``send_text`` never returns and tracks ``close`` calls."""

    def __init__(self) -> None:
        self.send_attempt_count = 0
        self.close_call_count = 0
        self.close_codes: list[int] = []

    async def send_text(self, text: str) -> None:
        self.send_attempt_count += 1
        # Hang indefinitely; callers must use asyncio.wait_for to bail out.
        await asyncio.Event().wait()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_call_count += 1
        self.close_codes.append(code)


@pytest.mark.timeout(5)
def test_run_ws_broadcast_loop_aborts_when_send_hangs(
    broadcaster: WebSocketBroadcaster,
    agent_manager: AgentManager,
) -> None:
    """A hung ``send_text`` triggers the timeout path, closes the socket, and unregisters the queue."""
    fake_websocket = _HangingWebSocket()

    # The fake websocket is intentionally not a starlette ``WebSocket`` -- the type
    # ignore is needed because we are passing a duck-typed test double.
    asyncio.run(
        _run_ws_broadcast_loop(
            websocket=fake_websocket,  # type: ignore[arg-type]
            agent_manager=agent_manager,
            ws_broadcaster=broadcaster,
            send_timeout_seconds=0.05,
        )
    )

    # Only the first send was attempted (it hung) and the socket was explicitly
    # closed with an internal-error code.
    assert fake_websocket.send_attempt_count == 1
    assert fake_websocket.close_call_count == 1
    assert fake_websocket.close_codes == [1011]

    # The handler's ``finally`` block must have unregistered its queue. We verify
    # behaviorally: a freshly-registered queue receives any subsequent broadcast
    # exactly once -- no zombie queue is also receiving.
    fresh_queue = broadcaster.register()
    broadcaster.broadcast({"type": "after_timeout"})
    received = fresh_queue.get_nowait()
    assert received is not None
    assert json.loads(received) == {"type": "after_timeout"}
    assert fresh_queue.empty()
