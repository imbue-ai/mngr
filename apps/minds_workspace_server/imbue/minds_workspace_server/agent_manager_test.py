"""Tests for the AgentManager."""

import json
import queue
import threading
from pathlib import Path

import pytest

from imbue.minds_workspace_server.agent_manager import AgentManager
from imbue.minds_workspace_server.agent_manager import _LogQueueCallback
from imbue.minds_workspace_server.models import AgentCreationError
from imbue.minds_workspace_server.models import AgentStateItem
from imbue.minds_workspace_server.models import ApplicationEntry
from imbue.minds_workspace_server.ws_broadcaster import WebSocketBroadcaster
from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import DiscoveryEventType
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import make_agent_discovery_event
from imbue.mngr.api.discovery_events import make_full_discovery_snapshot_event
from imbue.mngr.primitives import AgentId as MngrAgentId
from imbue.mngr.primitives import AgentName as MngrAgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName


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
    assert apps == []


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

    agent_manager._read_applications(toml_file)

    apps = agent_manager.get_applications()
    assert len(apps) == 2
    assert apps[0].name == "web"
    assert apps[0].url == "http://localhost:8000"
    assert apps[1].name == "terminal"
    assert apps[1].url == "http://localhost:7681"


def test_read_applications_handles_missing_file(agent_manager: AgentManager, tmp_path: Path) -> None:
    toml_file = tmp_path / "nonexistent.toml"
    agent_manager._read_applications(toml_file)

    apps = agent_manager.get_applications()
    assert apps == []


def test_read_applications_handles_empty_file(agent_manager: AgentManager, tmp_path: Path) -> None:
    toml_file = tmp_path / "empty.toml"
    toml_file.write_text("")
    agent_manager._read_applications(toml_file)

    apps = agent_manager.get_applications()
    assert apps == []


def test_read_applications_ignores_entries_without_name(agent_manager: AgentManager, tmp_path: Path) -> None:
    toml_content = """
[[applications]]
url = "http://localhost:8000"
"""
    toml_file = tmp_path / "applications.toml"
    toml_file.write_text(toml_content)

    agent_manager._read_applications(toml_file)

    apps = agent_manager.get_applications()
    assert apps == []


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
        agent_manager._applications = [
            ApplicationEntry(name="web", url="http://localhost:8000"),
        ]

    serialized = agent_manager.get_applications_serialized()
    assert serialized == [{"name": "web", "url": "http://localhost:8000"}]


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
    """The proto_agent_created broadcast fires before the creation thread runs."""
    q = broadcaster.register()

    agent_id = agent_manager.create_chat_agent("test-chat")
    agent_manager.stop()

    assert isinstance(agent_id, str)
    assert len(agent_id) > 0

    raw = q.get_nowait()
    assert raw is not None
    proto_msg = json.loads(raw)
    assert proto_msg["type"] == "proto_agent_created"
    assert proto_msg["agent_id"] == agent_id
    assert proto_msg["creation_type"] == "chat"
    assert proto_msg["parent_agent_id"] is None


def test_create_worktree_agent_broadcasts_proto_created(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, git_work_dir: Path
) -> None:
    """The proto_agent_created broadcast fires before the creation thread runs."""
    q = broadcaster.register()

    with agent_manager._lock:
        agent_manager._agents["parent-id"] = AgentStateItem(
            id="parent-id",
            name="parent",
            state="RUNNING",
            labels={},
            work_dir=str(git_work_dir),
        )

    agent_id = agent_manager.create_worktree_agent("test-worktree", "parent-id")
    agent_manager.stop()

    assert isinstance(agent_id, str)

    raw = q.get_nowait()
    assert raw is not None
    proto_msg = json.loads(raw)
    assert proto_msg["type"] == "proto_agent_created"
    assert proto_msg["creation_type"] == "worktree"
    assert proto_msg["parent_agent_id"] is None


def test_get_log_queue_for_proto_agent(agent_manager: AgentManager, git_work_dir: Path) -> None:
    """The log queue is available immediately after create_worktree_agent returns."""
    with agent_manager._lock:
        agent_manager._agents["parent-id"] = AgentStateItem(
            id="parent-id",
            name="parent",
            state="RUNNING",
            labels={},
            work_dir=str(git_work_dir),
        )

    agent_id = agent_manager.create_worktree_agent("test-worktree", "parent-id")
    log_q = agent_manager.get_log_queue(agent_id)
    assert log_q is not None

    agent_manager.stop()


def test_get_log_queue_returns_none_for_unknown(agent_manager: AgentManager) -> None:
    assert agent_manager.get_log_queue("nonexistent") is None


def test_stop_without_start(agent_manager: AgentManager) -> None:
    """Stopping an agent manager that was never started is safe."""
    agent_manager.stop()


def test_handle_agent_discovered(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
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


def test_agent_destroyed_removes_agent(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
    """Destroying an agent removes it from the tracked list and broadcasts."""
    test_agent_id = MngrAgentId()
    str_id = str(test_agent_id)
    q = broadcaster.register()

    agent = DiscoveredAgent(
        host_id=HostId(),
        agent_id=test_agent_id,
        agent_name=MngrAgentName("doomed"),
        provider_name=ProviderInstanceName("local"),
        certified_data={"labels": {}, "work_dir": None},
    )
    snapshot_with_agent = make_full_discovery_snapshot_event([agent], [])
    agent_manager._handle_full_snapshot(snapshot_with_agent)
    assert len(agent_manager.get_agents()) == 1

    q.get_nowait()

    snapshot_without_agent = make_full_discovery_snapshot_event([], [])
    agent_manager._handle_full_snapshot(snapshot_without_agent)

    agents = agent_manager.get_agents()
    assert len(agents) == 0

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "agents_updated"
    assert str_id not in [a["id"] for a in msg["agents"]]


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
    assert len(apps) == 1
    assert apps[0].name == "web"

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "applications_updated"


def test_read_applications_handles_invalid_toml(agent_manager: AgentManager, tmp_path: Path) -> None:
    """Invalid TOML files are handled gracefully."""
    toml_file = tmp_path / "bad.toml"
    toml_file.write_text("this is [[ not valid toml {{")

    agent_manager._read_applications(toml_file)

    apps = agent_manager.get_applications()
    assert apps == []


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


def test_initial_discover_populates_agents(
    broadcaster: WebSocketBroadcaster,
) -> None:
    """Initial discovery populates agent list when discovery succeeds."""
    manager = AgentManager.build(broadcaster)
    manager._initial_discover()


def test_initial_discover_handles_errors(
    broadcaster: WebSocketBroadcaster,
) -> None:
    """Initial discovery handles errors gracefully when mngr is unavailable."""
    manager = AgentManager.build(broadcaster)
    manager._initial_discover()
    assert isinstance(manager.get_agents(), list)


def test_refresh_agents_does_not_crash(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
    """Refresh agents handles errors gracefully and does not raise."""
    agent_manager._refresh_agents()
    assert isinstance(agent_manager.get_agents(), list)


def test_handle_full_snapshot(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
    """Full snapshot events replace the entire agent list."""
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


def test_run_creation_logs_header_and_completion(agent_manager: AgentManager, tmp_path: Path) -> None:
    """Creation thread logs a header line and a done message."""
    log_q: queue.Queue[str | None] = queue.Queue(maxsize=10000)
    cmd = ["true"]

    done_event = threading.Event()

    def run_and_signal() -> None:
        agent_manager._run_creation("test-id", "test-agent", cmd, tmp_path, log_q, {})
        done_event.set()

    t = threading.Thread(target=run_and_signal, daemon=True)
    t.start()
    done_event.wait(timeout=10)

    messages = [json.loads(item) for item in iter(log_q.get_nowait, None)]

    assert any("line" in m and str(tmp_path) in m["line"] for m in messages)
    done_msgs = [m for m in messages if "done" in m]
    assert len(done_msgs) == 1
    assert done_msgs[0]["success"] is True


def test_log_queue_callback_puts_json_line(
    agent_manager: AgentManager,
) -> None:
    """_LogQueueCallback writes each line as a JSON object to the queue."""
    q: queue.Queue[str | None] = queue.Queue()
    cb = _LogQueueCallback(log_queue=q)
    cb("hello\n", True)

    item = q.get_nowait()
    assert item is not None
    assert json.loads(item) == {"line": "hello"}


def test_handle_observe_output_line_empty_is_ignored(agent_manager: AgentManager) -> None:
    """Empty lines from the observe subprocess are silently ignored."""
    agent_manager._handle_observe_output_line("   ", True)
    assert agent_manager.get_agents() == []


def test_handle_observe_output_line_invalid_json_is_ignored(agent_manager: AgentManager) -> None:
    """Non-JSON output from the observe subprocess is ignored."""
    agent_manager._handle_observe_output_line("not json {", True)
    assert agent_manager.get_agents() == []


def test_handle_observe_output_line_dispatches_agent_discovered(
    agent_manager: AgentManager,
) -> None:
    """Valid agent-discovered JSONL lines are parsed and dispatched."""
    test_agent_id = MngrAgentId()
    agent = DiscoveredAgent(
        host_id=HostId(),
        agent_id=test_agent_id,
        agent_name=MngrAgentName("obs-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data={"labels": {}, "work_dir": None},
    )
    event = make_agent_discovery_event(agent)
    line = event.model_dump_json()

    agent_manager._handle_observe_output_line(line, True)

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].id == str(test_agent_id)


def test_handle_discovery_event_dispatches_full_snapshot(
    agent_manager: AgentManager,
) -> None:
    """FullDiscoverySnapshotEvent events are dispatched to _handle_full_snapshot."""
    test_agent_id = MngrAgentId()
    agent = DiscoveredAgent(
        host_id=HostId(),
        agent_id=test_agent_id,
        agent_name=MngrAgentName("snap-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data={"labels": {}, "work_dir": None},
    )
    event = make_full_discovery_snapshot_event([agent], [])
    agent_manager._handle_discovery_event(event)

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].id == str(test_agent_id)


def test_handle_discovery_event_dispatches_agent_discovered(
    agent_manager: AgentManager,
) -> None:
    """AgentDiscoveryEvent events are dispatched to _handle_agent_discovered."""
    test_agent_id = MngrAgentId()
    agent = DiscoveredAgent(
        host_id=HostId(),
        agent_id=test_agent_id,
        agent_name=MngrAgentName("disc-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data={"labels": {}, "work_dir": None},
    )
    event = make_agent_discovery_event(agent)
    agent_manager._handle_discovery_event(event)

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].id == str(test_agent_id)


def _make_agent_destroyed_event(agent_id: MngrAgentId, host_id: HostId) -> AgentDestroyedEvent:
    """Build an AgentDestroyedEvent for testing."""
    return AgentDestroyedEvent.model_validate(
        {
            "timestamp": "2026-01-01T00:00:00.000000000Z",
            "type": DiscoveryEventType.AGENT_DESTROYED,
            "event_id": "test-event-id",
            "source": "mngr/discovery",
            "agent_id": str(agent_id),
            "host_id": str(host_id),
        }
    )


def _make_host_destroyed_event(host_id: HostId, agent_ids: list[MngrAgentId]) -> HostDestroyedEvent:
    """Build a HostDestroyedEvent for testing."""
    return HostDestroyedEvent.model_validate(
        {
            "timestamp": "2026-01-01T00:00:00.000000000Z",
            "type": DiscoveryEventType.HOST_DESTROYED,
            "event_id": "test-event-id",
            "source": "mngr/discovery",
            "host_id": str(host_id),
            "agent_ids": [str(a) for a in agent_ids],
        }
    )


def test_handle_agent_destroyed_removes_agent(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
    """_handle_agent_destroyed removes the agent and broadcasts the update."""
    test_agent_id = MngrAgentId()
    host_id = HostId()
    q = broadcaster.register()

    with agent_manager._lock:
        agent_manager._agents[str(test_agent_id)] = AgentStateItem(
            id=str(test_agent_id),
            name="to-destroy",
            state="RUNNING",
            labels={},
            work_dir=None,
        )

    event = _make_agent_destroyed_event(test_agent_id, host_id)
    agent_manager._handle_agent_destroyed(event)

    assert len(agent_manager.get_agents()) == 0
    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "agents_updated"


def test_handle_discovery_event_dispatches_agent_destroyed(
    agent_manager: AgentManager,
) -> None:
    """AgentDestroyedEvent events are dispatched to _handle_agent_destroyed."""
    test_agent_id = MngrAgentId()
    host_id = HostId()
    with agent_manager._lock:
        agent_manager._agents[str(test_agent_id)] = AgentStateItem(
            id=str(test_agent_id),
            name="to-destroy",
            state="RUNNING",
            labels={},
            work_dir=None,
        )

    event = _make_agent_destroyed_event(test_agent_id, host_id)
    agent_manager._handle_discovery_event(event)
    assert len(agent_manager.get_agents()) == 0


def test_handle_host_destroyed_removes_all_agents(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """_handle_host_destroyed removes all agents on the host and broadcasts."""
    agent_id_1 = MngrAgentId()
    agent_id_2 = MngrAgentId()
    host_id = HostId()
    q = broadcaster.register()

    with agent_manager._lock:
        for aid in (agent_id_1, agent_id_2):
            agent_manager._agents[str(aid)] = AgentStateItem(
                id=str(aid),
                name=f"agent-{str(aid)[:8]}",
                state="RUNNING",
                labels={},
                work_dir=None,
            )

    event = _make_host_destroyed_event(host_id, [agent_id_1, agent_id_2])
    agent_manager._handle_host_destroyed(event)

    assert len(agent_manager.get_agents()) == 0
    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "agents_updated"


def test_handle_discovery_event_dispatches_host_destroyed(
    agent_manager: AgentManager,
) -> None:
    """HostDestroyedEvent events are dispatched to _handle_host_destroyed."""
    agent_id = MngrAgentId()
    host_id = HostId()
    with agent_manager._lock:
        agent_manager._agents[str(agent_id)] = AgentStateItem(
            id=str(agent_id),
            name="host-agent",
            state="RUNNING",
            labels={},
            work_dir=None,
        )

    event = _make_host_destroyed_event(host_id, [agent_id])
    agent_manager._handle_discovery_event(event)
    assert len(agent_manager.get_agents()) == 0
