"""Tests for the FastAPI server."""

import json
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from imbue.claude_web_chat.config import Config
from imbue.claude_web_chat.server import create_application


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

    with patch("imbue.claude_web_chat.server.STATIC_DIRECTORY", static_dir):
        test_app = create_application()
        test_client = TestClient(test_app)
        response = test_client.get("/")
        assert response.status_code == 200
        assert "test" in response.text


def test_index_returns_not_built_when_no_static(client: TestClient, tmp_path: Path) -> None:
    """When static dir has no index.html, show a helpful message."""
    empty_dir = tmp_path / "static"
    empty_dir.mkdir()

    with patch("imbue.claude_web_chat.server.STATIC_DIRECTORY", empty_dir):
        test_app = create_application()
        test_client = TestClient(test_app)
        response = test_client.get("/")
        assert response.status_code == 200
        assert "npm run build" in response.text


def test_list_agents_endpoint(client: TestClient) -> None:
    """The agents endpoint returns agent data."""
    with patch("imbue.claude_web_chat.server.discover_agents") as mock_discover:
        from imbue.claude_web_chat.agent_discovery import AgentInfo

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
    with patch("imbue.claude_web_chat.server.discover_agents", return_value=[]):
        response = client.get("/api/agents/nonexistent/events")
    assert response.status_code == 404


def test_send_message_for_unknown_agent(client: TestClient) -> None:
    """Sending a message to a nonexistent agent returns 404."""
    with patch("imbue.claude_web_chat.server.discover_agents", return_value=[]):
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

    with patch("imbue.claude_web_chat.server.discover_agents") as mock_discover:
        from imbue.claude_web_chat.agent_discovery import AgentInfo

        mock_discover.return_value = [
            AgentInfo(
                id="agent-123",
                name="test-agent",
                state="RUNNING",
                agent_state_dir=agent_state_dir,
                claude_config_dir=claude_config_dir,
            )
        ]
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
    with (
        patch("imbue.claude_web_chat.server.discover_agents") as mock_discover,
        patch("imbue.claude_web_chat.server.send_message", return_value=True) as mock_send,
    ):
        from imbue.claude_web_chat.agent_discovery import AgentInfo

        mock_discover.return_value = [
            AgentInfo(
                id="agent-123",
                name="test-agent",
                state="RUNNING",
                agent_state_dir=Path("/tmp/test"),
                claude_config_dir=Path("/tmp/.claude"),
            )
        ]
        response = client.post("/api/agents/agent-123/message", json={"message": "hello"})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    mock_send.assert_called_once_with("test-agent", "hello")
