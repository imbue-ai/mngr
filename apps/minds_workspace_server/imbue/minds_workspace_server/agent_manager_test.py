"""Tests for the AgentManager."""

import json
import queue
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest
from watchdog.events import FileModifiedEvent
from watchdog.events import FileMovedEvent

from imbue.minds_workspace_server.activity_state import ActivityState
from imbue.minds_workspace_server.activity_watcher import PERMISSIONS_WAITING_MARKER_FILENAME
from imbue.minds_workspace_server.agent_manager import AgentManager
from imbue.minds_workspace_server.agent_manager import _LogQueueCallback
from imbue.minds_workspace_server.agent_manager import _make_applications_file_handler
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
from imbue.mngr.utils.polling import poll_until

# Several tests in this module spin up real watchdog FSEvents observers
# (via ``_ensure_marker_watcher`` and ``_start_app_watcher``). On macOS the
# FSEvents emitter thread occasionally stalls during shutdown, tripping
# pytest-timeout. Mark the whole file as flaky so offload retries it
# automatically -- mirrors ``ws_broadcaster_test.py``.
pytestmark = pytest.mark.flaky


def _seed_agent(manager: AgentManager, agent_id: str) -> None:
    """Insert a placeholder ``AgentStateItem`` directly into the tracked map."""
    with manager._lock:
        manager._agents[agent_id] = AgentStateItem(
            id=agent_id,
            name=f"agent-{agent_id}",
            state="RUNNING",
            labels={},
            work_dir=None,
        )


def _drain(q: queue.Queue[str | None]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while not q.empty():
        raw = q.get_nowait()
        if raw is None:
            break
        out.append(json.loads(raw))
    return out


def _last_agents_updated(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if message.get("type") == "agents_updated":
            return message
    return None


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
    assert serialized[0]["activity_state"] is None


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


@pytest.mark.flaky
def test_start_app_watcher(agent_manager: AgentManager, tmp_path: Path) -> None:
    """Starting an app watcher for an agent creates the runtime directory."""
    runtime_dir = tmp_path / "runtime"
    agent_manager._start_app_watcher("watcher-test", tmp_path)
    assert runtime_dir.exists()
    agent_manager._stop_app_watcher("watcher-test")


def test_applications_file_handler_fires_on_move(tmp_path: Path) -> None:
    """The applications watcher must react to move/rename events, not just
    modify events. scripts/forward_port.py writes applications.toml atomically
    via ``tempfile.mkstemp`` + ``os.replace``, which surfaces as an
    ``IN_MOVED_TO`` / ``FileMovedEvent`` in watchdog -- if the handler only
    listened on ``on_modified`` every service registration after startup
    would be silently dropped.
    """
    seen: list[str] = []
    handler = _make_applications_file_handler("agent-x", lambda aid: seen.append(aid))

    # Simulate what os.replace(tmp, applications.toml) surfaces as.
    handler.dispatch(
        FileMovedEvent(
            src_path=str(tmp_path / "applications.toml.tmp"),
            dest_path=str(tmp_path / "applications.toml"),
        )
    )

    assert seen == ["agent-x"]


def test_applications_file_handler_ignores_unrelated_paths(tmp_path: Path) -> None:
    """The handler must not fire for writes to forward_port.py's scratch
    ``applications.toml.*.tmp`` files. Every upsert creates and modifies one
    of those before the atomic rename, and firing on each would produce a
    broadcast storm with no useful information (the scratch file is never
    the source of truth we read).
    """
    seen: list[str] = []
    handler = _make_applications_file_handler("agent-x", lambda aid: seen.append(aid))

    handler.dispatch(FileModifiedEvent(src_path=str(tmp_path / "applications.toml.abc123.tmp")))

    assert seen == []


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


def test_handle_observe_output_line_raises_on_invalid_json(agent_manager: AgentManager) -> None:
    """Invalid JSON on stdout from mngr observe surfaces as JSONDecodeError so the upstream bug is visible."""
    with pytest.raises(json.JSONDecodeError):
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


def test_build_observe_command_honors_injected_binary(broadcaster: WebSocketBroadcaster) -> None:
    """The ``mngr_binary`` argument to ``build()`` overrides the default binary path."""
    manager = AgentManager.build(broadcaster, mngr_binary="/path/to/custom-mngr")
    try:
        cmd = manager._build_observe_command()
        assert cmd[0] == "/path/to/custom-mngr"
    finally:
        manager.stop()


def test_resolve_observe_cwd_prefers_existing_work_dir(
    broadcaster: WebSocketBroadcaster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``MNGR_AGENT_WORK_DIR`` points at a real directory, observe runs there."""
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    manager = AgentManager.build(broadcaster)
    try:
        assert manager._resolve_observe_cwd() == tmp_path
    finally:
        manager.stop()


def test_resolve_observe_cwd_falls_back_when_work_dir_missing(
    broadcaster: WebSocketBroadcaster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``MNGR_AGENT_WORK_DIR`` is set but the path does not exist, use ``$HOME``.

    Guards the fallback that keeps observe runnable in tests that stub the env
    var with a non-existent path (e.g. the shared ``agent_manager`` fixture).
    """
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(missing))
    manager = AgentManager.build(broadcaster)
    try:
        assert manager._resolve_observe_cwd() == Path.home()
    finally:
        manager.stop()


def test_resolve_observe_cwd_falls_back_when_work_dir_unset(
    broadcaster: WebSocketBroadcaster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``MNGR_AGENT_WORK_DIR`` unset, observe runs from ``$HOME``."""
    monkeypatch.delenv("MNGR_AGENT_WORK_DIR", raising=False)
    manager = AgentManager.build(broadcaster)
    try:
        assert manager._resolve_observe_cwd() == Path.home()
    finally:
        manager.stop()


def test_start_observe_spawns_long_lived_subprocess(
    broadcaster: WebSocketBroadcaster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the observe subprocess stays alive after startup.

    A healthy ``mngr observe`` keeps running until it is explicitly stopped;
    this test asserts that after ``_start_observe`` returns, the child is
    still running a short window later rather than having exited on its own.
    """
    if shutil.which("mngr") is None:
        pytest.skip("mngr binary not on PATH")

    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    # Point the subprocess at a clean cwd with no project-local .mngr/settings.toml;
    # otherwise running pytest from inside a mngr-managed worktree would inherit
    # a config with ``is_allowed_in_pytest = false`` and the child would abort.
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    manager = AgentManager.build(broadcaster)
    try:
        manager._start_observe()
        assert manager._observe_process is not None
        # If the subprocess exits within the window it's a failure (bad command,
        # crashed on startup, etc.). A healthy observe keeps running.
        exited = poll_until(
            lambda: manager._observe_process is not None and manager._observe_process.poll() is not None,
            timeout=1.5,
            poll_interval=0.1,
        )
        assert not exited, (
            "mngr observe subprocess exited within 1.5s of startup "
            f"(returncode={manager._observe_process.returncode}); stderr: "
            f"{manager._observe_process.read_stderr()!r}"
        )
    finally:
        manager.stop()


def test_start_observe_logs_error_when_subprocess_exits_unexpectedly(
    broadcaster: WebSocketBroadcaster,
    false_binary: str,
    loguru_records: list[str],
) -> None:
    """If the observe subprocess exits on its own, the watchdog logs an ERROR.

    Uses ``/usr/bin/false`` (or equivalent) as a stand-in mngr binary so the
    spawned process exits immediately with a non-zero code.
    """
    manager = AgentManager.build(broadcaster, mngr_binary=false_binary)
    try:
        manager._start_observe()
        logged_error = poll_until(
            lambda: any(r.startswith("ERROR") and "mngr observe" in r for r in loguru_records),
            timeout=5.0,
            poll_interval=0.05,
        )
        assert logged_error, (
            "Expected an ERROR log from the observe watchdog; got: "
            f"{[r for r in loguru_records if r.startswith('ERROR')]}"
        )
    finally:
        manager.stop()


def test_start_observe_watchdog_stays_quiet_on_clean_shutdown(
    broadcaster: WebSocketBroadcaster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[str],
) -> None:
    """Calling ``stop()`` on a healthy observe subprocess must not produce errors."""
    if shutil.which("mngr") is None:
        pytest.skip("mngr binary not on PATH")

    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    # See test_start_observe_spawns_long_lived_subprocess for why this is needed.
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    manager = AgentManager.build(broadcaster)
    manager._start_observe()
    # ``_start_observe`` only returns after ``run_process_in_background``
    # has spawned the child and its RunningProcess thread has started, so the
    # subprocess is guaranteed to be running by the time we call stop().
    assert manager._observe_process is not None
    manager.stop()

    errors = [r for r in loguru_records if r.startswith("ERROR") and "mngr observe" in r]
    assert errors == [], f"Watchdog logged errors during clean shutdown: {errors}"


def test_handle_observe_output_line_logs_stderr_as_warning(
    agent_manager: AgentManager,
    loguru_records: list[str],
) -> None:
    """Stderr output from the observe subprocess is surfaced as a warning."""
    agent_manager._handle_observe_output_line("something bad happened", is_stdout=False)

    warnings = [r for r in loguru_records if r.startswith("WARNING") and "mngr observe stderr" in r]
    assert warnings, f"Expected a stderr warning; got: {loguru_records}"
    assert "something bad happened" in warnings[0]


# ---------------------------------------------------------------------------
# Activity-state integration
# ---------------------------------------------------------------------------


def test_ensure_marker_watcher_skips_when_state_dir_missing(agent_manager: AgentManager) -> None:
    """No watcher is started for an agent whose host_dir state directory is absent."""
    _seed_agent(agent_manager, "remote-agent")
    agent_manager._ensure_marker_watcher("remote-agent")
    try:
        with agent_manager._lock:
            assert "remote-agent" not in agent_manager._marker_watchers
    finally:
        agent_manager.stop()


def test_ensure_marker_watcher_seeds_idle_state_silently(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """When the state dir exists with no markers, the agent is seeded as IDLE without broadcasting."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")

    listener = broadcaster.register()
    try:
        agent_manager._ensure_marker_watcher("agent-1")
        # No broadcast should have happened (lifecycle handlers broadcast separately).
        with pytest.raises(queue.Empty):
            listener.get_nowait()

        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.IDLE
            assert agent_manager._agents["agent-1"].activity_state == ActivityState.IDLE.value
    finally:
        agent_manager.stop()


def test_session_events_user_message_drives_thinking(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A user_message at the tail of the transcript flips activity_state to THINKING.

    Replaces the old behavior where THINKING was driven by a transient ``active``
    marker file -- that marker could leak past the end of a turn and falsely
    pin the indicator on "Thinking...". Transcript content is now authoritative.
    """
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_marker_watcher("agent-1")

    listener = broadcaster.register()
    try:
        agent_manager.update_session_events(
            "agent-1",
            [{"type": "user_message", "content": "go"}],
        )
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.THINKING
        latest = _last_agents_updated(_drain(listener))
        assert latest is not None
        agents = latest["agents"]
        assert isinstance(agents, list)
        assert agents[0]["activity_state"] == ActivityState.THINKING.value
    finally:
        agent_manager.stop()


def test_session_events_assistant_message_at_tail_is_idle(agent_manager: AgentManager, tmp_path: Path) -> None:
    """An assistant_message with no pending tools at the tail means IDLE."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_marker_watcher("agent-1")

    try:
        agent_manager.update_session_events(
            "agent-1",
            [
                {"type": "user_message", "content": "go"},
                {"type": "assistant_message", "tool_calls": []},
            ],
        )
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.IDLE
    finally:
        agent_manager.stop()


def test_permissions_marker_overrides_thinking(agent_manager: AgentManager, tmp_path: Path) -> None:
    """A live permissions_waiting marker overrides transcript-derived THINKING."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_marker_watcher("agent-1")

    try:
        agent_manager.update_session_events(
            "agent-1",
            [{"type": "user_message", "content": "go"}],
        )
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.THINKING

        (state_dir / PERMISSIONS_WAITING_MARKER_FILENAME).touch()
        agent_manager._on_markers_changed("agent-1")
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.WAITING_ON_PERMISSION
    finally:
        agent_manager.stop()


def test_update_session_events_flips_to_tool_running(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_marker_watcher("agent-1")

    listener = broadcaster.register()
    try:
        events_with_pending: list[dict[str, Any]] = [
            {
                "type": "assistant_message",
                "tool_calls": [{"tool_call_id": "call_a", "tool_name": "Bash"}],
            }
        ]
        agent_manager.update_session_events("agent-1", events_with_pending)

        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.TOOL_RUNNING

        latest = _last_agents_updated(_drain(listener))
        assert latest is not None
        agents = latest["agents"]
        assert isinstance(agents, list)
        assert agents[0]["activity_state"] == ActivityState.TOOL_RUNNING.value

        # Once the result lands, we flip to THINKING (last event is tool_result,
        # no pending tool_use remains).
        events_resolved = events_with_pending + [{"type": "tool_result", "tool_call_id": "call_a"}]
        agent_manager.update_session_events("agent-1", events_resolved)
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.THINKING
    finally:
        agent_manager.stop()


def test_update_session_events_no_op_when_no_watcher(agent_manager: AgentManager) -> None:
    """Calling update_session_events for an unknown agent is a quiet no-op.

    Beyond not raising, it must leave no residue in the per-agent caches:
    otherwise those entries would never be cleared (``_stop_marker_watcher``
    only fires for agents that previously had a watcher), accumulating
    indefinitely.
    """
    agent_manager.update_session_events(
        "ghost",
        [{"type": "assistant_message", "tool_calls": [{"tool_call_id": "x", "tool_name": "Bash"}]}],
    )
    with agent_manager._lock:
        assert "ghost" not in agent_manager._activity_state_by_agent
        assert "ghost" not in agent_manager._has_unmatched_tool_use_by_agent
        assert "ghost" not in agent_manager._last_event_type_by_agent


def test_stop_marker_watcher_clears_caches(agent_manager: AgentManager, tmp_path: Path) -> None:
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_marker_watcher("agent-1")
    # Seed a non-default cached state so we can verify it's cleared.
    agent_manager.update_session_events(
        "agent-1",
        [{"type": "user_message", "content": "go"}],
    )

    with agent_manager._lock:
        assert "agent-1" in agent_manager._marker_watchers
        assert "agent-1" in agent_manager._activity_state_by_agent
        assert "agent-1" in agent_manager._has_unmatched_tool_use_by_agent
        assert "agent-1" in agent_manager._last_event_type_by_agent

    agent_manager._stop_marker_watcher("agent-1")

    with agent_manager._lock:
        assert "agent-1" not in agent_manager._marker_watchers
        assert "agent-1" not in agent_manager._activity_state_by_agent
        assert "agent-1" not in agent_manager._has_unmatched_tool_use_by_agent
        assert "agent-1" not in agent_manager._last_event_type_by_agent


def test_handle_agent_destroyed_stops_marker_watcher(agent_manager: AgentManager, tmp_path: Path) -> None:
    """An AGENT_DESTROYED event should clear the marker watcher and caches."""
    test_agent_id = MngrAgentId()
    host_id = HostId()
    str_id = str(test_agent_id)

    state_dir = tmp_path / "agents" / str_id
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, str_id)
    agent_manager._ensure_marker_watcher(str_id)
    with agent_manager._lock:
        assert str_id in agent_manager._marker_watchers

    event = _make_agent_destroyed_event(test_agent_id, host_id)
    agent_manager._handle_agent_destroyed(event)

    with agent_manager._lock:
        assert str_id not in agent_manager._marker_watchers
        assert str_id not in agent_manager._activity_state_by_agent


def test_full_snapshot_preserves_activity_state_for_existing_watcher(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A FullDiscoverySnapshot must not wipe the activity_state of agents that
    already have a marker watcher.

    Regression test: ``_handle_full_snapshot`` rebuilds ``_agents`` from the
    raw discovery payload (which has no ``activity_state`` field), then calls
    ``_ensure_marker_watcher`` per agent. Previously, the watcher-already-
    exists branch returned early and skipped the recompute, so the broadcast
    that follows the snapshot emitted ``activity_state=None`` for every
    previously-tracked agent and the chat panel indicator briefly disappeared.
    """
    test_agent_id = MngrAgentId()
    str_id = str(test_agent_id)

    state_dir = tmp_path / "agents" / str_id
    state_dir.mkdir(parents=True)

    # First, simulate the agent already being tracked with a live watcher
    # whose transcript signals THINKING (a user_message with no reply).
    discovered = DiscoveredAgent(
        host_id=HostId(),
        agent_id=test_agent_id,
        agent_name=MngrAgentName("snapshot-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data={"labels": {}, "work_dir": str(tmp_path / "work")},
    )
    agent_manager._handle_agent_discovered(make_agent_discovery_event(discovered))
    agent_manager.update_session_events(str_id, [{"type": "user_message", "content": "go"}])
    with agent_manager._lock:
        assert agent_manager._activity_state_by_agent[str_id] == ActivityState.THINKING
        assert agent_manager._agents[str_id].activity_state == ActivityState.THINKING.value

    # Now drain prior broadcasts so the snapshot's broadcast is the only one
    # we read.
    listener = broadcaster.register()
    try:
        snapshot_event = make_full_discovery_snapshot_event([discovered], [])
        agent_manager._handle_full_snapshot(snapshot_event)

        latest = _last_agents_updated(_drain(listener))
        assert latest is not None
        agents = latest["agents"]
        assert isinstance(agents, list)
        # The broadcast must carry the cached activity_state, not None.
        assert agents[0]["id"] == str_id
        assert agents[0]["activity_state"] == ActivityState.THINKING.value

        with agent_manager._lock:
            assert agent_manager._agents[str_id].activity_state == ActivityState.THINKING.value
    finally:
        agent_manager.stop()
