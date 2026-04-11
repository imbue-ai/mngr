"""Tests for the AgentManager."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from imbue.mngr.api.discovery_events import make_agent_discovery_event
from imbue.mngr.primitives import AgentId as MngrAgentId
from imbue.mngr.primitives import AgentName as MngrAgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
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

    raw = q.get_nowait()
    assert raw is not None
    proto_msg = json.loads(raw)
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

    raw = q.get_nowait()
    assert raw is not None
    proto_msg = json.loads(raw)
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


def test_stop_without_start(agent_manager: AgentManager) -> None:
    """Stopping an agent manager that was never started is safe."""
    agent_manager.stop()


def test_handle_agent_discovered(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """Agent discovered events update the agent list and broadcast."""
    q = broadcaster.register()

    test_agent_id = MngrAgentId()
    agent = DiscoveredAgent(
        host_id=HostId(),
        agent_id=test_agent_id,
        agent_name=MngrAgentName("discovered-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data={"labels": {"user_created": "true"}, "work_dir": "/tmp/work"},
    )
    event = make_agent_discovery_event(agent)

    agent_manager._handle_agent_discovered(event)

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].id == str(test_agent_id)
    assert agents[0].name == "discovered-agent"

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "agents_updated"


def test_agent_destroyed_removes_agent(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """Removing an agent from the tracked list broadcasts the update."""
    test_agent_id = str(MngrAgentId())

    q = broadcaster.register()

    with agent_manager._lock:
        agent_manager._agents[test_agent_id] = AgentStateItem(
            id=test_agent_id,
            name="doomed",
            state="RUNNING",
            labels={},
            work_dir=None,
        )

    assert len(agent_manager.get_agents()) == 1

    with agent_manager._lock:
        agent_manager._agents.pop(test_agent_id, None)

    agent_manager._broadcaster.broadcast_agents_updated(
        agent_manager.get_agents_serialized()
    )

    agents = agent_manager.get_agents()
    assert len(agents) == 0

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "agents_updated"


def test_on_applications_changed(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """Application changes are detected and broadcast."""
    q = broadcaster.register()

    toml_path = tmp_path / "runtime" / "applications.toml"
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text('[[applications]]\nname = "web"\nurl = "http://localhost:8000"\n')

    with agent_manager._lock:
        agent_manager._agents["app-agent"] = AgentStateItem(
            id="app-agent",
            name="app-agent",
            state="RUNNING",
            labels={},
            work_dir=str(tmp_path),
        )

    agent_manager._on_applications_changed("app-agent")

    apps = agent_manager.get_applications()
    assert len(apps["app-agent"]) == 1
    assert apps["app-agent"][0].name == "web"

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "applications_updated"


def test_read_applications_handles_invalid_toml(
    agent_manager: AgentManager, tmp_path: Path
) -> None:
    """Invalid TOML files are handled gracefully."""
    toml_file = tmp_path / "bad.toml"
    toml_file.write_text("this is [[ not valid toml {{")

    agent_manager._read_applications("test-agent", toml_file)

    apps = agent_manager.get_applications()
    assert apps.get("test-agent") == []


def test_handle_discovery_event_ignores_unknown_types(agent_manager: AgentManager) -> None:
    """Unknown event types are ignored gracefully."""
    agent_manager._handle_discovery_event("not-a-real-event")


def test_create_worktree_raises_for_unknown_agent(agent_manager: AgentManager) -> None:
    """Creating a worktree for an unknown agent raises."""
    with pytest.raises(AgentCreationError, match="Cannot determine work directory"):
        agent_manager.create_worktree_agent("test", "nonexistent")


def test_start_app_watcher(agent_manager: AgentManager, tmp_path: Path) -> None:
    """Starting an app watcher for an agent creates the runtime directory."""
    runtime_dir = tmp_path / "runtime"
    agent_manager._start_app_watcher("watcher-test", tmp_path)
    assert runtime_dir.exists()
    agent_manager._stop_app_watcher("watcher-test")


def test_stop_app_watcher_nonexistent(agent_manager: AgentManager) -> None:
    """Stopping a watcher for an agent that isn't watched is safe."""
    agent_manager._stop_app_watcher("nonexistent")


def test_initial_discover_handles_errors(agent_manager: AgentManager) -> None:
    """Initial discovery handles errors gracefully."""
    with patch(
        "imbue.minds_workspace_server.agent_manager.discover_agents",
        side_effect=RuntimeError("test error"),
    ):
        agent_manager._initial_discover()
    assert agent_manager.get_agents() == []


def test_refresh_agents_updates_state(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """Refresh agents updates the agent list and broadcasts."""
    from imbue.minds_workspace_server.agent_discovery import AgentInfo

    q = broadcaster.register()

    mock_agents = [
        AgentInfo(
            id="refreshed-1",
            name="refreshed-agent",
            state="RUNNING",
            agent_state_dir=Path("/tmp/state"),
            claude_config_dir=Path("/tmp/.claude"),
        )
    ]
    with patch(
        "imbue.minds_workspace_server.agent_manager.discover_agents",
        return_value=mock_agents,
    ):
        agent_manager._refresh_agents()

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].id == "refreshed-1"

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "agents_updated"


def test_handle_full_snapshot(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """Full snapshot events replace the entire agent list."""
    from imbue.mngr.api.discovery_events import make_full_discovery_snapshot_event

    q = broadcaster.register()

    agent1 = DiscoveredAgent(
        host_id=HostId(),
        agent_id=MngrAgentId(),
        agent_name=MngrAgentName("agent-one"),
        provider_name=ProviderInstanceName("local"),
        certified_data={"labels": {}, "work_dir": "/tmp/w1"},
    )
    agent2 = DiscoveredAgent(
        host_id=HostId(),
        agent_id=MngrAgentId(),
        agent_name=MngrAgentName("agent-two"),
        provider_name=ProviderInstanceName("local"),
        certified_data={"labels": {}, "work_dir": "/tmp/w2"},
    )
    event = make_full_discovery_snapshot_event([agent1, agent2], [])

    agent_manager._handle_full_snapshot(event)

    agents = agent_manager.get_agents()
    assert len(agents) == 2

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "agents_updated"
    assert len(msg["agents"]) == 2
