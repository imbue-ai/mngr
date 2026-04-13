"""Tests for the FastAPI server."""

import json
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from imbue.minds_workspace_server.agent_discovery import AgentInfo
from imbue.minds_workspace_server.config import Config
from imbue.minds_workspace_server.server import create_application


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


def test_get_layout_returns_404_for_unknown_agent(client: TestClient) -> None:
    """Getting layout for a nonexistent agent returns 404."""
    with patch("imbue.minds_workspace_server.server.discover_agents", return_value=[]):
        response = client.get("/api/agents/nonexistent/layout")
    assert response.status_code == 404


def test_get_layout_returns_404_when_no_layout_saved(client: TestClient, tmp_path: Path) -> None:
    """Getting layout returns 404 when no layout file exists."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()

    agent_info = AgentInfo(
        id="agent-123",
        name="test-agent",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=Path("/tmp/.claude"),
    )
    with patch("imbue.minds_workspace_server.server._find_agent", return_value=agent_info):
        response = client.get("/api/agents/agent-123/layout")

    assert response.status_code == 404


def test_save_and_get_layout(client: TestClient, tmp_path: Path) -> None:
    """Saving and retrieving a layout round-trips correctly."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()

    layout_data = {"dockview": {"panels": {}}, "panelParams": {"chat-1": {"panelType": "chat"}}}

    agent_info = AgentInfo(
        id="agent-123",
        name="test-agent",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=Path("/tmp/.claude"),
    )
    with patch("imbue.minds_workspace_server.server._find_agent", return_value=agent_info):
        save_response = client.post("/api/agents/agent-123/layout", json=layout_data)
        assert save_response.status_code == 200
        assert save_response.json()["status"] == "ok"

        get_response = client.get("/api/agents/agent-123/layout")
        assert get_response.status_code == 200
        assert get_response.json() == layout_data


def test_save_layout_creates_directory(client: TestClient, tmp_path: Path) -> None:
    """Saving a layout creates the workspace_layout directory if needed."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()

    agent_info = AgentInfo(
        id="agent-123",
        name="test-agent",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=Path("/tmp/.claude"),
    )
    with patch("imbue.minds_workspace_server.server._find_agent", return_value=agent_info):
        client.post("/api/agents/agent-123/layout", json={"test": True})

    assert (agent_state_dir / "workspace_layout" / "layout.json").exists()


def test_save_layout_rejects_invalid_json(client: TestClient, tmp_path: Path) -> None:
    """Saving invalid JSON returns 400."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()

    agent_info = AgentInfo(
        id="agent-123",
        name="test-agent",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=Path("/tmp/.claude"),
    )
    with patch("imbue.minds_workspace_server.server._find_agent", return_value=agent_info):
        response = client.post(
            "/api/agents/agent-123/layout",
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


def test_create_chat_agent_missing_parent(client: TestClient) -> None:
    """Creating a chat agent with an unknown parent returns 400."""
    response = client.post(
        "/api/agents/create-chat",
        json={"name": "test-chat", "parent_agent_id": "nonexistent"},
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
