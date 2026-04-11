"""Tests for the AgentManager."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from imbue.minds_workspace_server.agent_manager import AgentManager
from imbue.minds_workspace_server.models import AgentCreationError
from imbue.minds_workspace_server.models import AgentStateItem
from imbue.minds_workspace_server.models import ApplicationEntry
from imbue.minds_workspace_server.ws_broadcaster import WebSocketBroadcaster


@pytest.fixture
def broadcaster() -> WebSocketBroadcaster:
    return WebSocketBroadcaster()


@pytest.fixture
def agent_manager(broadcaster: WebSocketBroadcaster) -> AgentManager:
    """Create an AgentManager without starting observe subprocess."""
    with patch.dict(os.environ, {"MNGR_AGENT_ID": "test-agent-id", "MNGR_AGENT_WORK_DIR": "/tmp/test-work"}):
        manager = AgentManager(broadcaster)
    return manager


def test_generate_random_name(agent_manager: AgentManager) -> None:
    name = agent_manager.generate_random_name()
    assert isinstance(name, str)
    assert len(name) > 0
    assert "-" in name


def test_get_agents_initially_empty(agent_manager: AgentManager) -> None:
    agents = agent_manager.get_agents()
    assert agents == []


def test_get_applications_initially_empty(agent_manager: AgentManager) -> None:
    apps = agent_manager.get_applications()
    assert apps == {}


def test_get_proto_agents_initially_empty(agent_manager: AgentManager) -> None:
    protos = agent_manager.get_proto_agents()
    assert protos == []


def test_read_applications_parses_toml(agent_manager: AgentManager, tmp_path: Path) -> None:
    toml_content = """
[[applications]]
name = "web"
url = "http://localhost:8000"

[[applications]]
name = "terminal"
url = "http://localhost:7681"
"""
    toml_file = tmp_path / "applications.toml"
    toml_file.write_text(toml_content)

    agent_manager._read_applications("test-agent", toml_file)

    apps = agent_manager.get_applications()
    assert "test-agent" in apps
    assert len(apps["test-agent"]) == 2
    assert apps["test-agent"][0].name == "web"
    assert apps["test-agent"][0].url == "http://localhost:8000"
    assert apps["test-agent"][1].name == "terminal"
    assert apps["test-agent"][1].url == "http://localhost:7681"


def test_read_applications_handles_missing_file(agent_manager: AgentManager, tmp_path: Path) -> None:
    toml_file = tmp_path / "nonexistent.toml"
    agent_manager._read_applications("test-agent", toml_file)

    apps = agent_manager.get_applications()
    assert apps.get("test-agent") == []


def test_read_applications_handles_empty_file(agent_manager: AgentManager, tmp_path: Path) -> None:
    toml_file = tmp_path / "empty.toml"
    toml_file.write_text("")
    agent_manager._read_applications("test-agent", toml_file)

    apps = agent_manager.get_applications()
    assert apps.get("test-agent") == []


def test_read_applications_ignores_entries_without_name(agent_manager: AgentManager, tmp_path: Path) -> None:
    toml_content = """
[[applications]]
url = "http://localhost:8000"
"""
    toml_file = tmp_path / "applications.toml"
    toml_file.write_text(toml_content)

    agent_manager._read_applications("test-agent", toml_file)

    apps = agent_manager.get_applications()
    assert apps.get("test-agent") == []


def test_get_agents_serialized(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        agent_manager._agents["a1"] = AgentStateItem(
            id="a1",
            name="agent-one",
            state="RUNNING",
            labels={"user_created": "true"},
            work_dir="/tmp/work",
        )

    serialized = agent_manager.get_agents_serialized()
    assert len(serialized) == 1
    assert serialized[0]["id"] == "a1"
    assert serialized[0]["name"] == "agent-one"
    assert serialized[0]["labels"] == {"user_created": "true"}


def test_get_applications_serialized(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        agent_manager._applications["a1"] = [
            ApplicationEntry(name="web", url="http://localhost:8000"),
        ]

    serialized = agent_manager.get_applications_serialized()
    assert serialized == {"a1": [{"name": "web", "url": "http://localhost:8000"}]}


def test_resolve_agent_work_dir_from_own_env(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        result = agent_manager._resolve_agent_work_dir("test-agent-id")
    assert result == "/tmp/test-work"


def test_resolve_agent_work_dir_from_tracked_agent(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        agent_manager._agents["other-agent"] = AgentStateItem(
            id="other-agent",
            name="other",
            state="RUNNING",
            labels={},
            work_dir="/tmp/other-work",
        )
        result = agent_manager._resolve_agent_work_dir("other-agent")
    assert result == "/tmp/other-work"


def test_resolve_agent_work_dir_returns_none_for_unknown(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        result = agent_manager._resolve_agent_work_dir("unknown-id")
    assert result is None


def test_create_chat_agent_broadcasts_proto_created(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    from imbue.minds_workspace_server.models import AgentStateItem

    q = broadcaster.register()

    with agent_manager._lock:
        agent_manager._agents["parent-id"] = AgentStateItem(
            id="parent-id",
            name="parent",
            state="RUNNING",
            labels={},
            work_dir="/tmp/test-work",
        )

    with patch("imbue.minds_workspace_server.agent_manager.subprocess.Popen"):
        agent_id = agent_manager.create_chat_agent("test-chat", "parent-id")

    assert isinstance(agent_id, str)
    assert len(agent_id) > 0

    proto_msg = json.loads(q.get_nowait())
    assert proto_msg["type"] == "proto_agent_created"
    assert proto_msg["agent_id"] == agent_id
    assert proto_msg["creation_type"] == "chat"
    assert proto_msg["parent_agent_id"] == "parent-id"


def test_create_chat_agent_raises_for_unknown_parent(agent_manager: AgentManager) -> None:
    with pytest.raises(AgentCreationError, match="Cannot determine work directory"):
        agent_manager.create_chat_agent("test-chat", "nonexistent-parent")


def test_create_worktree_agent_broadcasts_proto_created(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    from imbue.minds_workspace_server.models import AgentStateItem

    q = broadcaster.register()

    with agent_manager._lock:
        agent_manager._agents["parent-id"] = AgentStateItem(
            id="parent-id",
            name="parent",
            state="RUNNING",
            labels={},
            work_dir="/tmp/test-work",
        )

    with (
        patch("imbue.minds_workspace_server.agent_manager.subprocess.Popen"),
        patch("imbue.minds_workspace_server.agent_manager.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(stdout="main\n", returncode=0)
        agent_id = agent_manager.create_worktree_agent("test-worktree", "parent-id")

    assert isinstance(agent_id, str)

    proto_msg = json.loads(q.get_nowait())
    assert proto_msg["type"] == "proto_agent_created"
    assert proto_msg["creation_type"] == "worktree"
    assert proto_msg["parent_agent_id"] is None


def test_get_log_queue_for_proto_agent(
    agent_manager: AgentManager,
) -> None:
    with agent_manager._lock:
        agent_manager._agents["parent-id"] = AgentStateItem(
            id="parent-id",
            name="parent",
            state="RUNNING",
            labels={},
            work_dir="/tmp/test-work",
        )

    with (
        patch("imbue.minds_workspace_server.agent_manager.subprocess.Popen"),
        patch("imbue.minds_workspace_server.agent_manager.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(stdout="main\n", returncode=0)
        agent_id = agent_manager.create_worktree_agent("test-worktree", "parent-id")

    log_q = agent_manager.get_log_queue(agent_id)
    assert log_q is not None


def test_get_log_queue_returns_none_for_unknown(agent_manager: AgentManager) -> None:
    assert agent_manager.get_log_queue("nonexistent") is None
