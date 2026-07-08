import json
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.api.discovery_events import DISCOVERY_EVENT_SOURCE
from imbue.mngr.api.discovery_events import DiscoveredProvider
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.api.discovery_events import DiscoveryErrorEvent
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import _make_envelope_fields
from imbue.mngr.api.discovery_events import make_discovered_provider
from imbue.mngr.api.discovery_events import make_provider_discovery_snapshot_event
from imbue.mngr.api.observe import AGENT_STATES_EVENT_SOURCE
from imbue.mngr.api.observe import AgentObserver
from imbue.mngr.api.observe import AgentStateChangeEvent
from imbue.mngr.api.observe import AgentStateEvent
from imbue.mngr.api.observe import FullAgentStateEvent
from imbue.mngr.api.observe import OBSERVE_EVENT_SOURCE
from imbue.mngr.api.observe import ObserveEventType
from imbue.mngr.api.observe import ObserveLockError
from imbue.mngr.api.observe import _TrackedState
from imbue.mngr.api.observe import _make_unknown_agent_details
from imbue.mngr.api.observe import acquire_observe_lock
from imbue.mngr.api.observe import append_agent_state_change_event
from imbue.mngr.api.observe import append_observe_event
from imbue.mngr.api.observe import get_agent_states_events_dir
from imbue.mngr.api.observe import get_agent_states_events_path
from imbue.mngr.api.observe import get_default_events_base_dir
from imbue.mngr.api.observe import get_observe_events_dir
from imbue.mngr.api.observe import get_observe_events_path
from imbue.mngr.api.observe import get_observe_lock_path
from imbue.mngr.api.observe import load_base_state_from_history
from imbue.mngr.api.observe import make_agent_state_change_event
from imbue.mngr.api.observe import make_agent_state_event
from imbue.mngr.api.observe import make_full_agent_state_event
from imbue.mngr.api.observe import release_observe_lock
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr.utils.testing import make_test_agent_details
from imbue.mngr.utils.testing import make_test_discovered_agent
from imbue.mngr.utils.testing import make_test_discovered_host

# === Path Helper Tests ===


def test_get_default_events_base_dir_expands_home(temp_config: MngrConfig) -> None:
    events_base_dir = get_default_events_base_dir(temp_config)
    assert events_base_dir == temp_config.default_host_dir.expanduser()


def test_get_observe_events_dir_returns_correct_path(temp_host_dir: Path) -> None:
    events_dir = get_observe_events_dir(temp_host_dir)
    assert events_dir == temp_host_dir / "events" / "mngr" / "agents"


def test_get_observe_events_path_returns_jsonl_file(temp_host_dir: Path) -> None:
    events_path = get_observe_events_path(temp_host_dir)
    assert events_path.name == "events.jsonl"
    assert events_path.parent.name == "agents"


def test_get_agent_states_events_dir_returns_correct_path(temp_host_dir: Path) -> None:
    events_dir = get_agent_states_events_dir(temp_host_dir)
    assert events_dir == temp_host_dir / "events" / "mngr" / "agent_states"


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
    assert data["source"] == "mngr/agents"


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
    assert data["source"] == "mngr/agent_states"
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

    with capture_loguru(level="WARNING") as log_output:
        tracked = load_base_state_from_history(temp_host_dir)
    assert len(tracked) == 1
    assert tracked[str(agent.id)].agent_state == "RUNNING"
    # Mid-file corruption (followed by another line) should surface as a warning
    assert "Skipped corrupt JSONL line" in log_output.getvalue()


def test_load_base_state_from_history_silent_on_partial_last_line(temp_host_dir: Path) -> None:
    events_path = get_observe_events_path(temp_host_dir)
    events_path.parent.mkdir(parents=True, exist_ok=True)

    agent = make_test_agent_details(state=AgentLifecycleState.RUNNING)
    event = make_full_agent_state_event([agent])
    event_json = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))

    with open(events_path, "w") as f:
        f.write(event_json + "\n")
        # Last line: a partial write at EOF (no trailing newline, malformed JSON)
        f.write("incomplete{")

    with capture_loguru(level="WARNING") as log_output:
        tracked = load_base_state_from_history(temp_host_dir)
    assert len(tracked) == 1
    assert tracked[str(agent.id)].agent_state == "RUNNING"
    assert log_output.getvalue() == ""


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
    assert parsed["source"] == "mngr/agents"
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
    assert parsed["source"] == "mngr/agent_states"
    assert parsed["old_state"] == "STOPPED"
    assert parsed["new_state"] == "RUNNING"
    assert parsed["old_host_state"] == "RUNNING"
    assert parsed["new_host_state"] == "RUNNING"
    assert parsed["agent"]["name"] == "test-agent"


# === AgentObserver Tests ===


def _make_observer(temp_mngr_ctx: MngrContext, noop_binary: str) -> AgentObserver:
    """Create an AgentObserver with events_base_dir derived from the test config."""
    return AgentObserver(
        mngr_ctx=temp_mngr_ctx,
        events_base_dir=get_default_events_base_dir(temp_mngr_ctx.config),
        mngr_binary=noop_binary,
    )


def _make_provider_snapshot_line(
    provider_name: ProviderInstanceName,
    agents: Sequence[DiscoveredAgent] = (),
    hosts: Sequence[DiscoveredHost] = (),
    provider: DiscoveredProvider | None = None,
    error: DiscoveryError | None = None,
    unknown_host_ids: Sequence[HostId] = (),
) -> str:
    """Serialize a per-provider discovery snapshot event to a JSONL line for the discovery stream."""
    now = datetime.now(timezone.utc)
    event = make_provider_discovery_snapshot_event(
        provider_name,
        agents,
        hosts,
        discovery_started_at=now,
        discovery_finished_at=now,
        provider=provider,
        error=error,
        unknown_host_ids=unknown_host_ids,
    )
    return json.dumps(event.model_dump(mode="json"))


def _feed_provider_snapshot(
    observer: AgentObserver,
    provider_name: ProviderInstanceName,
    agents: Sequence[DiscoveredAgent] = (),
    hosts: Sequence[DiscoveredHost] = (),
    provider: DiscoveredProvider | None = None,
    error: DiscoveryError | None = None,
    unknown_host_ids: Sequence[HostId] = (),
) -> None:
    """Feed a per-provider discovery snapshot through the observer's discovery stream handler."""
    line = _make_provider_snapshot_line(
        provider_name,
        agents=agents,
        hosts=hosts,
        provider=provider,
        error=error,
        unknown_host_ids=unknown_host_ids,
    )
    observer._on_discovery_stream_output(line, is_stdout=True)


def test_agent_observer_provider_snapshot_tracks_hosts(temp_mngr_ctx: MngrContext, noop_binary: str) -> None:
    """A per-provider snapshot populates known hosts from its host records and starts activity streams."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    host1 = make_test_discovered_host()
    host2 = make_test_discovered_host()
    agent1 = make_test_discovered_agent()

    with observer._concurrency_group:
        _feed_provider_snapshot(observer, ProviderInstanceName("local"), agents=[agent1], hosts=[host1, host2])
        assert len(observer._known_hosts) == 2
        assert str(host1.host_id) in observer._known_hosts
        assert str(host2.host_id) in observer._known_hosts
        assert observer._known_hosts[str(host1.host_id)].host_name == host1.host_name
        # Newly-known hosts get activity streams started.
        assert str(host1.host_id) in observer._events_processes
        assert str(host2.host_id) in observer._events_processes


def test_agent_observer_provider_snapshot_removes_stale_hosts(temp_mngr_ctx: MngrContext, noop_binary: str) -> None:
    """A host present in a prior snapshot for the same provider is removed when absent from a new one."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    host_a = make_test_discovered_host()
    host_b = make_test_discovered_host()

    with observer._concurrency_group:
        _feed_provider_snapshot(observer, ProviderInstanceName("local"), hosts=[host_a])
        assert str(host_a.host_id) in observer._known_hosts

        _feed_provider_snapshot(observer, ProviderInstanceName("local"), hosts=[host_b])
        assert str(host_a.host_id) not in observer._known_hosts
        assert str(host_b.host_id) in observer._known_hosts
        # The dropped host's activity stream is stopped; the new host's is started.
        assert str(host_a.host_id) not in observer._events_processes
        assert str(host_b.host_id) in observer._events_processes


def test_agent_observer_provider_snapshot_scopes_removal_per_provider(
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """A snapshot for one provider does not remove hosts attributed to a different provider."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    local_host = make_test_discovered_host()
    modal_host = make_test_discovered_host()

    with observer._concurrency_group:
        _feed_provider_snapshot(observer, ProviderInstanceName("local"), hosts=[local_host])
        _feed_provider_snapshot(observer, ProviderInstanceName("modal"), hosts=[modal_host])
        assert str(local_host.host_id) in observer._known_hosts
        assert str(modal_host.host_id) in observer._known_hosts

        # A fresh empty snapshot for "modal" only drops modal's host; local's is untouched.
        _feed_provider_snapshot(observer, ProviderInstanceName("modal"), hosts=[])
        assert str(local_host.host_id) in observer._known_hosts
        assert str(modal_host.host_id) not in observer._known_hosts


def test_agent_observer_on_activity_event_queues_host(temp_mngr_ctx: MngrContext, noop_binary: str) -> None:
    """Verify that _on_activity_event adds the host to the activity queue."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    observer._on_activity_event('{"type":"SOME_EVENT"}', is_stdout=True, host_id_str="host-123")
    assert observer._activity_queue.qsize() == 1
    assert observer._activity_queue.get_nowait() == "host-123"


def test_agent_observer_on_activity_event_ignores_stderr(temp_mngr_ctx: MngrContext, noop_binary: str) -> None:
    """Verify that stderr output is ignored."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    observer._on_activity_event("some stderr", is_stdout=False, host_id_str="host-123")
    assert observer._activity_queue.qsize() == 0


def test_agent_observer_on_activity_event_ignores_empty_lines(temp_mngr_ctx: MngrContext, noop_binary: str) -> None:
    """Verify that empty/whitespace lines are ignored."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    observer._on_activity_event("", is_stdout=True, host_id_str="host-123")
    observer._on_activity_event("   \n", is_stdout=True, host_id_str="host-123")
    assert observer._activity_queue.qsize() == 0


def test_agent_observer_emit_agent_state_writes_event_to_file(temp_mngr_ctx: MngrContext, noop_binary: str) -> None:
    """Verify that _emit_agent_state writes an AGENT_STATE event to the events file."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    agent = make_test_agent_details(name="observed-agent")

    observer._emit_agent_state(agent)

    events_path = get_observe_events_path(observer.events_base_dir)
    assert events_path.exists()
    lines = events_path.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["type"] == "AGENT_STATE"
    assert data["agent"]["name"] == "observed-agent"


def test_agent_observer_emit_agent_state_updates_tracking(temp_mngr_ctx: MngrContext, noop_binary: str) -> None:
    """Verify that _emit_agent_state updates the last known state tracking."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    agent = make_test_agent_details()

    observer._emit_agent_state(agent)

    tracked = observer._last_tracked_state_by_id[str(agent.id)]
    assert tracked.agent_state == "RUNNING"
    assert tracked.host_state == "RUNNING"


def test_agent_observer_emit_agent_state_emits_state_change_for_new_agent(
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """Verify that _emit_agent_state emits a state change event for a newly seen agent."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
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
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """Verify that no state change event is emitted when the lifecycle state field is the same."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    agent = make_test_agent_details(state=AgentLifecycleState.RUNNING)

    # First emit triggers state change (None -> RUNNING)
    observer._emit_agent_state(agent)
    # Second emit with same state should not add another state change
    observer._emit_agent_state(agent)

    # Only the initial state change should be emitted (None -> RUNNING), not a duplicate
    states_path = get_agent_states_events_path(observer.events_base_dir)
    lines = states_path.read_text().strip().splitlines()
    assert len(lines) == 1


def test_agent_observer_emit_agent_state_detects_state_transition(
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """Verify that _emit_agent_state emits a state change when state transitions from a known value."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
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


def test_agent_observer_stop_sets_stop_event(temp_mngr_ctx: MngrContext, noop_binary: str) -> None:
    """Verify that stop() signals the observer to halt."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    assert not observer._stop_event.is_set()
    observer.stop()
    assert observer._stop_event.is_set()


def test_agent_observer_on_discovery_stream_output_ignores_non_stdout(
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """Verify that stderr output from the discovery stream is ignored."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    observer._on_discovery_stream_output("some error message", is_stdout=False)
    assert len(observer._known_hosts) == 0


def test_agent_observer_on_discovery_stream_output_raises_on_invalid_json(
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """Invalid JSON on the discovery stream surfaces as a JSONDecodeError so the upstream bug is visible."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    with pytest.raises(json.JSONDecodeError):
        observer._on_discovery_stream_output("not valid json at all", is_stdout=True)
    assert len(observer._known_hosts) == 0


def test_agent_observer_do_full_state_snapshot_writes_event(temp_mngr_ctx: MngrContext, noop_binary: str) -> None:
    """Verify that _do_full_state_snapshot writes an AGENTS_FULL_STATE event."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)

    observer._do_full_state_snapshot()

    events_path = get_observe_events_path(observer.events_base_dir)
    assert events_path.exists()
    lines = events_path.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["type"] == "AGENTS_FULL_STATE"


def test_agent_observer_process_snapshot_agents_emits_state_changes(
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """Verify that _process_snapshot_agents detects state field changes and emits to agent_states."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
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
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """Verify that _process_snapshot_agents does not emit a state change when state is unchanged."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
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
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """Verify that _emit_state_change writes an AGENT_STATE_CHANGE event to the agent_states file."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
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


def test_agent_observer_emit_agent_state_detects_host_state_change(
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """Verify that a state change event is emitted when host state changes but agent state stays the same."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
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
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """Verify that _process_snapshot_agents detects host state changes and emits to agent_states."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
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


def test_agent_observer_on_discovery_stream_output_handles_host_destroyed(
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """A HostDestroyedEvent on the discovery stream removes the host and stops its activity stream."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    host = make_test_discovered_host()

    with observer._concurrency_group:
        # Populate known_hosts (and start the activity stream) via a per-provider snapshot.
        _feed_provider_snapshot(observer, ProviderInstanceName("local"), hosts=[host])
        assert str(host.host_id) in observer._known_hosts
        assert str(host.host_id) in observer._events_processes

        # Feed a serialized HostDestroyedEvent through _on_discovery_stream_output.
        timestamp, event_id = _make_envelope_fields()
        destroyed_event = HostDestroyedEvent(
            timestamp=timestamp,
            event_id=event_id,
            source=DISCOVERY_EVENT_SOURCE,
            host_id=host.host_id,
            agent_ids=(),
        )
        line = json.dumps(destroyed_event.model_dump(mode="json"), separators=(",", ":"))
        observer._on_discovery_stream_output(line, is_stdout=True)
        assert str(host.host_id) not in observer._known_hosts
        assert str(host.host_id) not in observer._events_processes


# === UNKNOWN State Tests ===


def _make_provider(name: str) -> DiscoveredProvider:
    return make_discovered_provider(
        ProviderInstanceName(name),
        ProviderInstanceConfig(backend=ProviderBackendName("docker"), is_enabled=True),
    )


def test_make_unknown_agent_details_sets_state_unknown_and_preserves_identity() -> None:
    """Synthetic UNKNOWN AgentDetails keeps name/type/id but flips state + host.state to UNKNOWN."""
    original = make_test_agent_details(name="ghost-agent", state=AgentLifecycleState.RUNNING)
    unknown = _make_unknown_agent_details(original)

    assert unknown.id == original.id
    assert unknown.name == original.name
    assert unknown.type == original.type
    assert unknown.work_dir == original.work_dir
    assert unknown.host.id == original.host.id
    assert unknown.host.name == original.host.name
    assert unknown.host.provider_name == original.host.provider_name
    # Both states flip to UNKNOWN
    assert unknown.state == AgentLifecycleState.UNKNOWN
    assert unknown.host.state == HostState.UNKNOWN


def test_agent_observer_errored_provider_snapshot_records_errored_providers(
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """A per-provider snapshot carrying an error populates _currently_errored_providers and wakes the loop."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    errored = ProviderInstanceName("modal")

    with observer._concurrency_group:
        # A healthy provider snapshot establishes "local" as a known provider.
        _feed_provider_snapshot(observer, ProviderInstanceName("local"), provider=_make_provider("local"))
        assert not observer._snapshot_trigger.is_set()

        # An errored provider snapshot records the error and wakes the periodic loop.
        _feed_provider_snapshot(
            observer,
            errored,
            error=DiscoveryError(
                type_name="ImbueCloudAuthError",
                message="token missing",
                provider_name=errored,
            ),
        )
        assert observer._currently_errored_providers == {errored}
        assert observer._known_provider_names == {ProviderInstanceName("local"), errored}
        # Trigger should fire so the periodic loop wakes early.
        assert observer._snapshot_trigger.is_set()


def test_agent_observer_discovery_error_event_with_provider_name_adds_to_errored_set(
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """A DiscoveryErrorEvent with provider_name adds it to the errored set and triggers a snapshot."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    timestamp, event_id = _make_envelope_fields()
    event = DiscoveryErrorEvent(
        timestamp=timestamp,
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        error_type="ImbueCloudAuthError",
        error_message="auth failed",
        source_name="modal-prod",
        provider_name="modal-prod",
    )

    with observer._concurrency_group:
        line = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
        observer._on_discovery_stream_output(line, is_stdout=True)
        assert ProviderInstanceName("modal-prod") in observer._currently_errored_providers
        assert observer._snapshot_trigger.is_set()


def test_process_snapshot_agents_emits_unknown_when_provider_errored(
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """A previously-observed agent on an errored provider becomes UNKNOWN, not dropped."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    provider = ProviderInstanceName("modal")
    agent = make_test_agent_details(
        name="ghost",
        state=AgentLifecycleState.RUNNING,
        provider_name=provider,
    )

    with observer._concurrency_group:
        # First snapshot: agent observed
        observer._process_snapshot_agents([agent])
        # Mark provider as errored, then run snapshot with empty agent list
        observer._currently_errored_providers = {provider}
        observer._known_provider_names = {provider}
        observer._process_snapshot_agents([])

    # The full state event written for the second poll should contain a synthetic UNKNOWN
    events_path = get_observe_events_path(observer.events_base_dir)
    lines = events_path.read_text().strip().splitlines()
    # There should be exactly two FULL state events (one per call)
    assert len(lines) == 2
    second_event = json.loads(lines[1])
    assert second_event["type"] == "AGENTS_FULL_STATE"
    assert len(second_event["agents"]) == 1
    assert second_event["agents"][0]["state"] == "UNKNOWN"
    assert second_event["agents"][0]["host"]["state"] == "UNKNOWN"
    assert second_event["agents"][0]["id"] == str(agent.id)

    # A state change event should also have been emitted: RUNNING -> UNKNOWN
    states_path = get_agent_states_events_path(observer.events_base_dir)
    state_lines = states_path.read_text().strip().splitlines()
    transitions = [json.loads(line) for line in state_lines]
    assert any(
        t["old_state"] == "RUNNING" and t["new_state"] == "UNKNOWN" and t["agent_id"] == str(agent.id)
        for t in transitions
    )


def test_process_snapshot_agents_drops_agent_when_provider_removed_from_config(
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """If a previously-observed agent's provider is no longer in _known_provider_names, drop it."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    provider = ProviderInstanceName("modal")
    agent = make_test_agent_details(
        name="config-removed",
        state=AgentLifecycleState.RUNNING,
        provider_name=provider,
    )

    with observer._concurrency_group:
        observer._process_snapshot_agents([agent])
        assert str(agent.id) in observer._last_known_details_by_id
        # Provider no longer in known set; not in errored set either
        observer._known_provider_names = {ProviderInstanceName("local")}
        observer._currently_errored_providers = set()
        observer._process_snapshot_agents([])

    assert str(agent.id) not in observer._last_known_details_by_id
    assert str(agent.id) not in observer._last_tracked_state_by_id


def test_process_snapshot_agents_drops_agent_when_provider_healthy_and_agent_absent(
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """A previously-observed agent whose provider is healthy but who's missing from the listing is dropped (implicit destroy)."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    provider = ProviderInstanceName("local")
    agent = make_test_agent_details(
        name="implicit-destroyed",
        state=AgentLifecycleState.RUNNING,
        provider_name=provider,
    )

    with observer._concurrency_group:
        observer._process_snapshot_agents([agent])
        # Healthy provider, agent not in listing
        observer._known_provider_names = {provider}
        observer._currently_errored_providers = set()
        observer._process_snapshot_agents([])

    assert str(agent.id) not in observer._last_known_details_by_id


def test_process_snapshot_agents_unknown_is_sticky_until_reappearance(
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """An UNKNOWN agent leaves UNKNOWN only when it reappears in a snapshot."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    provider = ProviderInstanceName("modal")
    running_agent = make_test_agent_details(
        name="resurrecting",
        state=AgentLifecycleState.RUNNING,
        provider_name=provider,
    )

    with observer._concurrency_group:
        # Observe agent
        observer._process_snapshot_agents([running_agent])
        # Provider errors -- agent goes UNKNOWN
        observer._currently_errored_providers = {provider}
        observer._known_provider_names = {provider}
        observer._process_snapshot_agents([])
        assert observer._last_known_details_by_id[str(running_agent.id)].state == AgentLifecycleState.UNKNOWN
        # Provider stays errored; agent stays UNKNOWN (sticky)
        observer._process_snapshot_agents([])
        assert observer._last_known_details_by_id[str(running_agent.id)].state == AgentLifecycleState.UNKNOWN

        # Provider recovers and agent reappears
        observer._currently_errored_providers = set()
        observer._process_snapshot_agents([running_agent])
        assert observer._last_known_details_by_id[str(running_agent.id)].state == AgentLifecycleState.RUNNING


def test_process_snapshot_agents_unknown_scoped_to_errored_provider(
    temp_mngr_ctx: MngrContext, noop_binary: str
) -> None:
    """Only agents on a currently-errored provider go UNKNOWN; a healthy provider's absent agent is dropped."""
    observer = _make_observer(temp_mngr_ctx, noop_binary)
    errored_provider = ProviderInstanceName("modal")
    healthy_provider = ProviderInstanceName("local")
    errored_agent = make_test_agent_details(name="errored", provider_name=errored_provider)
    healthy_agent = make_test_agent_details(name="healthy", provider_name=healthy_provider)

    with observer._concurrency_group:
        observer._process_snapshot_agents([errored_agent, healthy_agent])
        # Only "modal" is errored this poll; both providers remain configured.
        observer._currently_errored_providers = {errored_provider}
        observer._known_provider_names = {errored_provider, healthy_provider}
        observer._process_snapshot_agents([])

    # The errored provider's agent is retained as UNKNOWN.
    assert observer._last_known_details_by_id[str(errored_agent.id)].state == AgentLifecycleState.UNKNOWN
    # The healthy provider's absent agent is dropped (implicit destroy).
    assert str(healthy_agent.id) not in observer._last_known_details_by_id
