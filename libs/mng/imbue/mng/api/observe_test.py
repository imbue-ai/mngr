import json
from pathlib import Path

import pytest

from imbue.mng.api.discovery_events import make_full_discovery_snapshot_event
from imbue.mng.api.observe import AGENT_STATES_EVENT_SOURCE
from imbue.mng.api.observe import AgentObserver
from imbue.mng.api.observe import AgentStateChangeEvent
from imbue.mng.api.observe import AgentStateEvent
from imbue.mng.api.observe import FullAgentStateEvent
from imbue.mng.api.observe import OBSERVE_EVENT_SOURCE
from imbue.mng.api.observe import ObserveEventType
from imbue.mng.api.observe import ObserveLockError
from imbue.mng.api.observe import _TrackedState
from imbue.mng.api.observe import acquire_observe_lock
from imbue.mng.api.observe import append_agent_state_change_event
from imbue.mng.api.observe import append_observe_event
from imbue.mng.api.observe import get_agent_states_events_dir
from imbue.mng.api.observe import get_agent_states_events_path
from imbue.mng.api.observe import get_default_events_base_dir
from imbue.mng.api.observe import get_observe_events_dir
from imbue.mng.api.observe import get_observe_events_path
from imbue.mng.api.observe import get_observe_lock_path
from imbue.mng.api.observe import load_base_state_from_history
from imbue.mng.api.observe import make_agent_state_change_event
from imbue.mng.api.observe import make_agent_state_event
from imbue.mng.api.observe import make_full_agent_state_event
from imbue.mng.api.observe import release_observe_lock
from imbue.mng.config.data_types import MngConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import HostState
from imbue.mng.utils.testing import make_test_agent_details
from imbue.mng.utils.testing import make_test_discovered_agent
from imbue.mng.utils.testing import make_test_discovered_host

# === Path Helper Tests ===


def test_get_default_events_base_dir_expands_home(temp_config: MngConfig) -> None:
    events_base_dir = get_default_events_base_dir(temp_config)
    assert events_base_dir == temp_config.default_host_dir.expanduser()


def test_get_observe_events_dir_returns_correct_path(temp_host_dir: Path) -> None:
    events_dir = get_observe_events_dir(temp_host_dir)
    assert events_dir == temp_host_dir / "events" / "mng" / "agents"


def test_get_observe_events_path_returns_jsonl_file(temp_host_dir: Path) -> None:
    events_path = get_observe_events_path(temp_host_dir)
    assert events_path.name == "events.jsonl"
    assert events_path.parent.name == "agents"


def test_get_agent_states_events_dir_returns_correct_path(temp_host_dir: Path) -> None:
    events_dir = get_agent_states_events_dir(temp_host_dir)
    assert events_dir == temp_host_dir / "events" / "mng" / "agent_states"


def test_get_agent_states_events_path_returns_jsonl_file(temp_host_dir: Path) -> None:
    events_path = get_agent_states_events_path(temp_host_dir)
    assert events_path.name == "events.jsonl"
    assert events_path.parent.name == "agent_states"


def test_get_observe_lock_path_returns_correct_path(temp_host_dir: Path) -> None:
    lock_path = get_observe_lock_path(temp_host_dir)
    assert lock_path == temp_host_dir / "observe_lock"


# === Event Construction Tests ===


def test_make_agent_state_event_has_correct_fields() -> None:
    agent = make_test_agent_details()
    event = make_agent_state_event(agent)
    assert event.type == ObserveEventType.AGENT_STATE
    assert event.source == OBSERVE_EVENT_SOURCE
    assert event.event_id.startswith("evt-")
    assert event.agent.name == "test-agent"
    assert isinstance(event, AgentStateEvent)


def test_make_full_agent_state_event_has_correct_fields() -> None:
    agents = [make_test_agent_details(name="agent-1"), make_test_agent_details(name="agent-2")]
    event = make_full_agent_state_event(agents)
    assert event.type == ObserveEventType.AGENTS_FULL_STATE
    assert event.source == OBSERVE_EVENT_SOURCE
    assert event.event_id.startswith("evt-")
    assert len(event.agents) == 2
    assert isinstance(event, FullAgentStateEvent)


def test_make_full_agent_state_event_with_empty_agents() -> None:
    event = make_full_agent_state_event([])
    assert event.type == ObserveEventType.AGENTS_FULL_STATE
    assert len(event.agents) == 0


def test_make_agent_state_change_event_has_correct_fields() -> None:
    agent = make_test_agent_details(state=AgentLifecycleState.RUNNING)
    event = make_agent_state_change_event(agent, "STOPPED", "RUNNING")
    assert event.type == ObserveEventType.AGENT_STATE_CHANGE
    assert event.source == AGENT_STATES_EVENT_SOURCE
    assert event.event_id.startswith("evt-")
    assert event.old_state == "STOPPED"
    assert event.new_state == "RUNNING"
    assert event.old_host_state == "RUNNING"
    assert event.new_host_state == "RUNNING"
    assert event.agent_id == agent.id
    assert event.agent_name == agent.name
    assert event.agent.name == "test-agent"
    assert isinstance(event, AgentStateChangeEvent)


def test_make_agent_state_change_event_with_none_old_state() -> None:
    agent = make_test_agent_details(state=AgentLifecycleState.RUNNING)
    event = make_agent_state_change_event(agent, None, None)
    assert event.old_state is None
    assert event.new_state == "RUNNING"
    assert event.old_host_state is None
    assert event.new_host_state == "RUNNING"


# === File I/O Tests ===


def test_append_observe_event_creates_file_and_writes_valid_json(temp_host_dir: Path) -> None:
    agent = make_test_agent_details()
    event = make_agent_state_event(agent)
    append_observe_event(temp_host_dir, event)

    events_path = get_observe_events_path(temp_host_dir)
    assert events_path.exists()

    lines = events_path.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["type"] == ObserveEventType.AGENT_STATE
    assert data["source"] == "mng/agents"


def test_append_observe_event_appends_multiple_events(temp_host_dir: Path) -> None:
    for idx in range(3):
        agent = make_test_agent_details(name=f"agent-{idx}")
        event = make_agent_state_event(agent)
        append_observe_event(temp_host_dir, event)

    events_path = get_observe_events_path(temp_host_dir)
    lines = events_path.read_text().strip().splitlines()
    assert len(lines) == 3


def test_append_observe_event_creates_parent_directories(temp_host_dir: Path) -> None:
    events_path = get_observe_events_path(temp_host_dir)
    assert not events_path.parent.exists()

    agent = make_test_agent_details()
    event = make_agent_state_event(agent)
    append_observe_event(temp_host_dir, event)
    assert events_path.parent.exists()


def test_append_agent_state_change_event_creates_file_and_writes_valid_json(temp_host_dir: Path) -> None:
    agent = make_test_agent_details(state=AgentLifecycleState.RUNNING)
    event = make_agent_state_change_event(agent, "STOPPED", "RUNNING")
    append_agent_state_change_event(temp_host_dir, event)

    events_path = get_agent_states_events_path(temp_host_dir)
    assert events_path.exists()

    lines = events_path.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["type"] == "AGENT_STATE_CHANGE"
    assert data["source"] == "mng/agent_states"
    assert data["old_state"] == "STOPPED"
    assert data["new_state"] == "RUNNING"
    assert data["old_host_state"] == "RUNNING"
    assert data["new_host_state"] == "RUNNING"


def test_append_agent_state_change_event_creates_parent_directories(temp_host_dir: Path) -> None:
    events_path = get_agent_states_events_path(temp_host_dir)
    assert not events_path.parent.exists()

    agent = make_test_agent_details(state=AgentLifecycleState.RUNNING)
    event = make_agent_state_change_event(agent, None, None)
    append_agent_state_change_event(temp_host_dir, event)
    assert events_path.parent.exists()


# === History Loading Tests ===


def test_load_base_state_from_history_returns_empty_when_no_file(temp_host_dir: Path) -> None:
    agent_state = load_base_state_from_history(temp_host_dir)
    assert agent_state == {}


def test_load_base_state_from_history_loads_latest_full_state(temp_host_dir: Path) -> None:
    agent1 = make_test_agent_details(name="agent-1", state=AgentLifecycleState.RUNNING)
    agent2 = make_test_agent_details(name="agent-2", state=AgentLifecycleState.STOPPED)
    event = make_full_agent_state_event([agent1, agent2])
    append_observe_event(temp_host_dir, event)

    tracked = load_base_state_from_history(temp_host_dir)
    assert len(tracked) == 2
    assert tracked[str(agent1.id)].agent_state == "RUNNING"
    assert tracked[str(agent1.id)].host_state == "RUNNING"
    assert tracked[str(agent2.id)].agent_state == "STOPPED"
    assert tracked[str(agent2.id)].host_state == "RUNNING"


def test_load_base_state_from_history_uses_latest_full_state(temp_host_dir: Path) -> None:
    agent1 = make_test_agent_details(name="agent-1", state=AgentLifecycleState.RUNNING)
    event1 = make_full_agent_state_event([agent1])
    append_observe_event(temp_host_dir, event1)

    agent2 = make_test_agent_details(name="agent-2", state=AgentLifecycleState.STOPPED)
    event2 = make_full_agent_state_event([agent2])
    append_observe_event(temp_host_dir, event2)

    tracked = load_base_state_from_history(temp_host_dir)
    assert len(tracked) == 1
    assert str(agent2.id) in tracked
    assert tracked[str(agent2.id)].agent_state == "STOPPED"


def test_load_base_state_from_history_ignores_non_full_state_events(temp_host_dir: Path) -> None:
    agent = make_test_agent_details(state=AgentLifecycleState.RUNNING)
    individual_event = make_agent_state_event(agent)
    append_observe_event(temp_host_dir, individual_event)

    agent_state = load_base_state_from_history(temp_host_dir)
    assert agent_state == {}


def test_load_base_state_from_history_handles_malformed_lines(temp_host_dir: Path) -> None:
    events_path = get_observe_events_path(temp_host_dir)
    events_path.parent.mkdir(parents=True, exist_ok=True)

    agent = make_test_agent_details(state=AgentLifecycleState.RUNNING)
    event = make_full_agent_state_event([agent])
    event_json = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))

    with open(events_path, "w") as f:
        f.write("not valid json\n")
        f.write(event_json + "\n")

    tracked = load_base_state_from_history(temp_host_dir)
    assert len(tracked) == 1
    assert tracked[str(agent.id)].agent_state == "RUNNING"


# === Lock Tests ===


def test_acquire_and_release_observe_lock(temp_host_dir: Path) -> None:
    fd = acquire_observe_lock(temp_host_dir)
    assert fd >= 0
    release_observe_lock(fd)


def test_acquire_observe_lock_fails_when_already_held(temp_host_dir: Path) -> None:
    fd = acquire_observe_lock(temp_host_dir)
    try:
        with pytest.raises(ObserveLockError):
            acquire_observe_lock(temp_host_dir)
    finally:
        release_observe_lock(fd)


def test_acquire_observe_lock_succeeds_after_release(temp_host_dir: Path) -> None:
    fd = acquire_observe_lock(temp_host_dir)
    release_observe_lock(fd)

    fd2 = acquire_observe_lock(temp_host_dir)
    release_observe_lock(fd2)


def test_observe_lock_creates_lock_file(temp_host_dir: Path) -> None:
    lock_path = get_observe_lock_path(temp_host_dir)
    assert not lock_path.exists()

    fd = acquire_observe_lock(temp_host_dir)
    assert lock_path.exists()
    release_observe_lock(fd)


def test_separate_dirs_can_lock_independently(tmp_path: Path) -> None:
    """Two different output directories can each hold a lock simultaneously."""
    dir_a = tmp_path / "observer-a"
    dir_a.mkdir()
    dir_b = tmp_path / "observer-b"
    dir_b.mkdir()

    fd_a = acquire_observe_lock(dir_a)
    fd_b = acquire_observe_lock(dir_b)
    release_observe_lock(fd_a)
    release_observe_lock(fd_b)


# === Serialization Roundtrip Tests ===


def test_agent_state_event_serializes_to_valid_json() -> None:
    agent = make_test_agent_details()
    event = make_agent_state_event(agent)
    data = event.model_dump(mode="json")
    json_str = json.dumps(data, separators=(",", ":"))

    parsed = json.loads(json_str)
    assert parsed["type"] == "AGENT_STATE"
    assert parsed["source"] == "mng/agents"
    assert "agent" in parsed
    assert parsed["agent"]["name"] == "test-agent"


def test_full_agent_state_event_serializes_to_valid_json() -> None:
    agents = [make_test_agent_details(name="a1"), make_test_agent_details(name="a2")]
    event = make_full_agent_state_event(agents)
    data = event.model_dump(mode="json")
    json_str = json.dumps(data, separators=(",", ":"))

    parsed = json.loads(json_str)
    assert parsed["type"] == "AGENTS_FULL_STATE"
    assert len(parsed["agents"]) == 2


def test_agent_state_change_event_serializes_to_valid_json() -> None:
    agent = make_test_agent_details(state=AgentLifecycleState.RUNNING)
    event = make_agent_state_change_event(agent, "STOPPED", "RUNNING")
    data = event.model_dump(mode="json")
    json_str = json.dumps(data, separators=(",", ":"))

    parsed = json.loads(json_str)
    assert parsed["type"] == "AGENT_STATE_CHANGE"
    assert parsed["source"] == "mng/agent_states"
    assert parsed["old_state"] == "STOPPED"
    assert parsed["new_state"] == "RUNNING"
    assert parsed["old_host_state"] == "RUNNING"
    assert parsed["new_host_state"] == "RUNNING"
    assert parsed["agent"]["name"] == "test-agent"


# === AgentObserver Tests ===


def _make_observer(temp_mng_ctx: MngContext, noop_binary: str) -> AgentObserver:
    """Create an AgentObserver with events_base_dir derived from the test config."""
    return AgentObserver(
        mng_ctx=temp_mng_ctx,
        events_base_dir=get_default_events_base_dir(temp_mng_ctx.config),
        mng_binary=noop_binary,
    )


def test_agent_observer_handle_full_snapshot_tracks_hosts(temp_mng_ctx: MngContext, noop_binary: str) -> None:
    """Verify that _handle_full_snapshot correctly populates known hosts from host records."""
    observer = _make_observer(temp_mng_ctx, noop_binary)
    host1 = make_test_discovered_host()
    host2 = make_test_discovered_host()
    agent1 = make_test_discovered_agent()

    snapshot = make_full_discovery_snapshot_event([agent1], [host1, host2])

    with observer._cg:
        observer._handle_full_snapshot(snapshot)
        assert len(observer._known_hosts) == 2
        assert str(host1.host_id) in observer._known_hosts
        assert str(host2.host_id) in observer._known_hosts
        assert observer._known_hosts[str(host1.host_id)].host_name == host1.host_name


def test_agent_observer_handle_full_snapshot_removes_stale_hosts(temp_mng_ctx: MngContext, noop_binary: str) -> None:
    """Verify that hosts from a prior snapshot are removed when not in a new snapshot."""
    observer = _make_observer(temp_mng_ctx, noop_binary)
    host_a = make_test_discovered_host()
    host_b = make_test_discovered_host()

    with observer._cg:
        snapshot1 = make_full_discovery_snapshot_event([], [host_a])
        observer._handle_full_snapshot(snapshot1)
        assert str(host_a.host_id) in observer._known_hosts

        snapshot2 = make_full_discovery_snapshot_event([], [host_b])
        observer._handle_full_snapshot(snapshot2)
        assert str(host_a.host_id) not in observer._known_hosts
        assert str(host_b.host_id) in observer._known_hosts


def test_agent_observer_on_activity_event_queues_host(temp_mng_ctx: MngContext, noop_binary: str) -> None:
    """Verify that _on_activity_event adds the host to the activity queue."""
    observer = _make_observer(temp_mng_ctx, noop_binary)
    observer._on_activity_event('{"type":"SOME_EVENT"}', is_stdout=True, host_id_str="host-123")
    assert observer._activity_queue.qsize() == 1
    assert observer._activity_queue.get_nowait() == "host-123"


def test_agent_observer_on_activity_event_ignores_stderr(temp_mng_ctx: MngContext, noop_binary: str) -> None:
    """Verify that stderr output is ignored."""
    observer = _make_observer(temp_mng_ctx, noop_binary)
    observer._on_activity_event("some stderr", is_stdout=False, host_id_str="host-123")
    assert observer._activity_queue.qsize() == 0


def test_agent_observer_on_activity_event_ignores_empty_lines(temp_mng_ctx: MngContext, noop_binary: str) -> None:
    """Verify that empty/whitespace lines are ignored."""
    observer = _make_observer(temp_mng_ctx, noop_binary)
    observer._on_activity_event("", is_stdout=True, host_id_str="host-123")
    observer._on_activity_event("   \n", is_stdout=True, host_id_str="host-123")
    assert observer._activity_queue.qsize() == 0


def test_agent_observer_emit_agent_state_writes_event_to_file(temp_mng_ctx: MngContext, noop_binary: str) -> None:
    """Verify that _emit_agent_state writes an AGENT_STATE event to the events file."""
    observer = _make_observer(temp_mng_ctx, noop_binary)
    agent = make_test_agent_details(name="observed-agent")

    observer._emit_agent_state(agent)

    events_path = get_observe_events_path(observer.events_base_dir)
    assert events_path.exists()
    lines = events_path.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["type"] == "AGENT_STATE"
    assert data["agent"]["name"] == "observed-agent"


def test_agent_observer_emit_agent_state_updates_tracking(temp_mng_ctx: MngContext, noop_binary: str) -> None:
    """Verify that _emit_agent_state updates the last known state tracking."""
    observer = _make_observer(temp_mng_ctx, noop_binary)
    agent = make_test_agent_details()

    observer._emit_agent_state(agent)

    tracked = observer._last_tracked_state_by_id[str(agent.id)]
    assert tracked.agent_state == "RUNNING"
    assert tracked.host_state == "RUNNING"


def test_agent_observer_emit_agent_state_emits_state_change_for_new_agent(
    temp_mng_ctx: MngContext, noop_binary: str
) -> None:
    """Verify that _emit_agent_state emits a state change event for a newly seen agent."""
    observer = _make_observer(temp_mng_ctx, noop_binary)
    agent = make_test_agent_details(name="new-agent", state=AgentLifecycleState.RUNNING)

    observer._emit_agent_state(agent)

    states_path = get_agent_states_events_path(observer.events_base_dir)
    assert states_path.exists()
    lines = states_path.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["type"] == "AGENT_STATE_CHANGE"
    assert data["old_state"] is None
    assert data["new_state"] == "RUNNING"
    assert data["agent_name"] == "new-agent"


def test_agent_observer_emit_agent_state_no_state_change_when_same_state(
    temp_mng_ctx: MngContext, noop_binary: str
) -> None:
    """Verify that no state change event is emitted when the lifecycle state field is the same."""
    observer = _make_observer(temp_mng_ctx, noop_binary)
    agent = make_test_agent_details(state=AgentLifecycleState.RUNNING)

    # First emit triggers state change (None -> RUNNING)
    observer._emit_agent_state(agent)
    # Second emit with same state should not add another state change
    observer._emit_agent_state(agent)

    # Only the initial state change should be emitted (None -> RUNNING), not a duplicate
    states_path = get_agent_states_events_path(observer.events_base_dir)
    lines = states_path.read_text().strip().splitlines()
    assert len(lines) == 1


def test_agent_observer_emit_agent_state_detects_state_transition(temp_mng_ctx: MngContext, noop_binary: str) -> None:
    """Verify that _emit_agent_state emits a state change when state transitions from a known value."""
    observer = _make_observer(temp_mng_ctx, noop_binary)
    agent_running = make_test_agent_details(name="transitioning", state=AgentLifecycleState.RUNNING)

    # First emit: None -> RUNNING
    observer._emit_agent_state(agent_running)

    # Second emit with a different state: RUNNING -> STOPPED
    agent_stopped = make_test_agent_details(name="transitioning", state=AgentLifecycleState.STOPPED)
    observer._last_tracked_state_by_id[str(agent_stopped.id)] = _TrackedState(
        agent_state="RUNNING", host_state="RUNNING"
    )
    observer._emit_agent_state(agent_stopped)

    states_path = get_agent_states_events_path(observer.events_base_dir)
    lines = states_path.read_text().strip().splitlines()
    assert len(lines) == 2

    # Second event should capture the RUNNING -> STOPPED transition
    data = json.loads(lines[1])
    assert data["type"] == "AGENT_STATE_CHANGE"
    assert data["old_state"] == "RUNNING"
    assert data["new_state"] == "STOPPED"
    assert data["old_host_state"] == "RUNNING"
    assert data["new_host_state"] == "RUNNING"
    assert data["agent_name"] == "transitioning"


def test_agent_observer_stop_sets_stop_event(temp_mng_ctx: MngContext, noop_binary: str) -> None:
    """Verify that stop() signals the observer to halt."""
    observer = _make_observer(temp_mng_ctx, noop_binary)
    assert not observer._stop_event.is_set()
    observer.stop()
    assert observer._stop_event.is_set()


def test_agent_observer_on_list_stream_output_ignores_non_stdout(temp_mng_ctx: MngContext, noop_binary: str) -> None:
    """Verify that stderr output from list --stream is ignored."""
    observer = _make_observer(temp_mng_ctx, noop_binary)
    observer._on_list_stream_output("some error message", is_stdout=False)
    assert len(observer._known_hosts) == 0


def test_agent_observer_on_list_stream_output_ignores_invalid_json(temp_mng_ctx: MngContext, noop_binary: str) -> None:
    """Verify that invalid JSON lines from list --stream are gracefully ignored."""
    observer = _make_observer(temp_mng_ctx, noop_binary)
    observer._on_list_stream_output("not valid json at all", is_stdout=True)
    assert len(observer._known_hosts) == 0


def test_agent_observer_do_full_state_snapshot_writes_event(temp_mng_ctx: MngContext, noop_binary: str) -> None:
    """Verify that _do_full_state_snapshot writes an AGENTS_FULL_STATE event."""
    observer = _make_observer(temp_mng_ctx, noop_binary)

    observer._do_full_state_snapshot()

    events_path = get_observe_events_path(observer.events_base_dir)
    assert events_path.exists()
    lines = events_path.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["type"] == "AGENTS_FULL_STATE"


def test_agent_observer_process_snapshot_agents_emits_state_changes(
    temp_mng_ctx: MngContext, noop_binary: str
) -> None:
    """Verify that _process_snapshot_agents detects state field changes and emits to agent_states."""
    observer = _make_observer(temp_mng_ctx, noop_binary)
    agent = make_test_agent_details(name="snapshot-agent", state=AgentLifecycleState.STOPPED)

    # Pre-populate with a different state to simulate a transition
    observer._last_tracked_state_by_id[str(agent.id)] = _TrackedState(agent_state="RUNNING", host_state="RUNNING")

    observer._process_snapshot_agents([agent])

    # Should have written a full state event
    events_path = get_observe_events_path(observer.events_base_dir)
    agents_lines = events_path.read_text().strip().splitlines()
    assert len(agents_lines) == 1
    assert json.loads(agents_lines[0])["type"] == "AGENTS_FULL_STATE"

    # Should have emitted a state change event (RUNNING -> STOPPED)
    states_path = get_agent_states_events_path(observer.events_base_dir)
    assert states_path.exists()
    states_lines = states_path.read_text().strip().splitlines()
    assert len(states_lines) == 1
    data = json.loads(states_lines[0])
    assert data["type"] == "AGENT_STATE_CHANGE"
    assert data["old_state"] == "RUNNING"
    assert data["new_state"] == "STOPPED"
    assert data["agent_name"] == "snapshot-agent"


def test_agent_observer_process_snapshot_agents_no_change_when_same_state(
    temp_mng_ctx: MngContext, noop_binary: str
) -> None:
    """Verify that _process_snapshot_agents does not emit a state change when state is unchanged."""
    observer = _make_observer(temp_mng_ctx, noop_binary)
    agent = make_test_agent_details(name="stable-agent", state=AgentLifecycleState.RUNNING)

    # Pre-populate with the same agent and host state
    observer._last_tracked_state_by_id[str(agent.id)] = _TrackedState(agent_state="RUNNING", host_state="RUNNING")

    observer._process_snapshot_agents([agent])

    # Full state event should still be written
    events_path = get_observe_events_path(observer.events_base_dir)
    agents_lines = events_path.read_text().strip().splitlines()
    assert len(agents_lines) == 1

    # No state change event should be written
    states_path = get_agent_states_events_path(observer.events_base_dir)
    assert not states_path.exists()


def test_agent_observer_emit_state_change_writes_to_agent_states_stream(
    temp_mng_ctx: MngContext, noop_binary: str
) -> None:
    """Verify that _emit_state_change writes an AGENT_STATE_CHANGE event to the agent_states file."""
    observer = _make_observer(temp_mng_ctx, noop_binary)
    agent = make_test_agent_details(name="transitioning-agent", state=AgentLifecycleState.STOPPED)

    observer._emit_state_change(agent, "RUNNING", "RUNNING")

    states_path = get_agent_states_events_path(observer.events_base_dir)
    assert states_path.exists()
    lines = states_path.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["type"] == "AGENT_STATE_CHANGE"
    assert data["old_state"] == "RUNNING"
    assert data["new_state"] == "STOPPED"
    assert data["old_host_state"] == "RUNNING"
    assert data["new_host_state"] == "RUNNING"
    assert data["agent_name"] == "transitioning-agent"


def test_agent_observer_emit_agent_state_detects_host_state_change(temp_mng_ctx: MngContext, noop_binary: str) -> None:
    """Verify that a state change event is emitted when host state changes but agent state stays the same."""
    observer = _make_observer(temp_mng_ctx, noop_binary)
    agent = make_test_agent_details(
        name="host-changing", state=AgentLifecycleState.RUNNING, host_state=HostState.PAUSED
    )

    # Pre-populate: agent was RUNNING on a RUNNING host
    observer._last_tracked_state_by_id[str(agent.id)] = _TrackedState(agent_state="RUNNING", host_state="RUNNING")

    observer._emit_agent_state(agent)

    states_path = get_agent_states_events_path(observer.events_base_dir)
    assert states_path.exists()
    lines = states_path.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["type"] == "AGENT_STATE_CHANGE"
    assert data["old_state"] == "RUNNING"
    assert data["new_state"] == "RUNNING"
    assert data["old_host_state"] == "RUNNING"
    assert data["new_host_state"] == "PAUSED"


def test_agent_observer_process_snapshot_agents_detects_host_state_change(
    temp_mng_ctx: MngContext, noop_binary: str
) -> None:
    """Verify that _process_snapshot_agents detects host state changes and emits to agent_states."""
    observer = _make_observer(temp_mng_ctx, noop_binary)
    agent = make_test_agent_details(
        name="host-transition-agent", state=AgentLifecycleState.RUNNING, host_state=HostState.PAUSED
    )

    # Pre-populate: same agent state, different host state
    observer._last_tracked_state_by_id[str(agent.id)] = _TrackedState(agent_state="RUNNING", host_state="RUNNING")

    observer._process_snapshot_agents([agent])

    states_path = get_agent_states_events_path(observer.events_base_dir)
    assert states_path.exists()
    states_lines = states_path.read_text().strip().splitlines()
    assert len(states_lines) == 1
    data = json.loads(states_lines[0])
    assert data["type"] == "AGENT_STATE_CHANGE"
    assert data["old_host_state"] == "RUNNING"
    assert data["new_host_state"] == "PAUSED"
    assert data["old_state"] == "RUNNING"
    assert data["new_state"] == "RUNNING"
