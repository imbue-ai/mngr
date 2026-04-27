"""Tests for WorkspaceServerHealthTracker (level-triggered)."""

from imbue.minds.desktop_client.workspace_server_health import AgentHealth
from imbue.minds.desktop_client.workspace_server_health import WorkspaceServerHealthTracker


def test_absent_agent_has_no_health() -> None:
    tracker = WorkspaceServerHealthTracker()
    assert tracker.get_health("agent-1") is None
    assert tracker.snapshot_all() == {}


def test_single_failure_marks_stuck() -> None:
    """A single connection-level failure is enough. No threshold to cross."""
    tracker = WorkspaceServerHealthTracker()
    tracker.record_failure("agent-1")
    assert tracker.get_health("agent-1") == AgentHealth.STUCK


def test_success_clears_stuck() -> None:
    tracker = WorkspaceServerHealthTracker()
    tracker.record_failure("agent-1")
    tracker.record_success("agent-1")
    assert tracker.get_health("agent-1") == AgentHealth.HEALTHY


def test_mark_restarting_sets_state() -> None:
    tracker = WorkspaceServerHealthTracker()
    tracker.mark_restarting("agent-1")
    assert tracker.get_health("agent-1") == AgentHealth.RESTARTING


def test_proxy_observation_clears_restarting() -> None:
    """A subsequent proxy success or failure overrides the restarting marker."""
    tracker = WorkspaceServerHealthTracker()
    tracker.mark_restarting("agent-1")
    tracker.record_success("agent-1")
    assert tracker.get_health("agent-1") == AgentHealth.HEALTHY

    tracker.mark_restarting("agent-2")
    tracker.record_failure("agent-2")
    assert tracker.get_health("agent-2") == AgentHealth.STUCK


def test_independent_agents_do_not_affect_each_other() -> None:
    tracker = WorkspaceServerHealthTracker()
    tracker.record_failure("agent-1")
    tracker.record_success("agent-2")
    assert tracker.get_health("agent-1") == AgentHealth.STUCK
    assert tracker.get_health("agent-2") == AgentHealth.HEALTHY
    assert tracker.snapshot_all() == {"agent-1": AgentHealth.STUCK, "agent-2": AgentHealth.HEALTHY}


def test_callback_fires_on_transition_only() -> None:
    """The callback fires once per actual transition, not on every observation."""
    tracker = WorkspaceServerHealthTracker()
    call_count = [0]

    def on_change() -> None:
        call_count[0] += 1

    tracker.add_on_change_callback(on_change)

    # First observation: None -> stuck is a transition.
    tracker.record_failure("agent-1")
    assert call_count[0] == 1

    # Second failure on an already-stuck agent: no transition.
    tracker.record_failure("agent-1")
    assert call_count[0] == 1

    # stuck -> healthy is a transition.
    tracker.record_success("agent-1")
    assert call_count[0] == 2

    # Subsequent healthy on an already-healthy agent: no transition.
    tracker.record_success("agent-1")
    assert call_count[0] == 2


def test_callback_fires_on_restarting_transition() -> None:
    tracker = WorkspaceServerHealthTracker()
    call_count = [0]

    def on_change() -> None:
        call_count[0] += 1

    tracker.record_failure("agent-1")
    tracker.add_on_change_callback(on_change)

    tracker.mark_restarting("agent-1")
    assert call_count[0] == 1

    # Marking restarting twice is a no-op.
    tracker.mark_restarting("agent-1")
    assert call_count[0] == 1


def test_remove_callback_is_silent_for_unknown_callable() -> None:
    tracker = WorkspaceServerHealthTracker()
    tracker.remove_on_change_callback(lambda: None)


def test_snapshot_is_a_copy() -> None:
    """Mutating the snapshot must not mutate the tracker's internal state."""
    tracker = WorkspaceServerHealthTracker()
    tracker.record_failure("agent-1")
    snapshot = tracker.snapshot_all()
    snapshot["agent-1"] = AgentHealth.HEALTHY
    assert tracker.get_health("agent-1") == AgentHealth.STUCK
