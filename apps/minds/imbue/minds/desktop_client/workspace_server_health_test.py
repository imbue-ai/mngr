"""Tests for WorkspaceServerHealthTracker."""

from imbue.minds.desktop_client.workspace_server_health import WorkspaceServerHealthTracker


def test_single_failure_is_not_stuck() -> None:
    tracker = WorkspaceServerHealthTracker()
    tracker.record_failure("agent-1", "system_interface", "TimeoutException")
    assert tracker.snapshot_stuck() == ()


def test_threshold_crossed_marks_server_stuck() -> None:
    tracker = WorkspaceServerHealthTracker(failure_threshold=3)
    for _ in range(3):
        tracker.record_failure("agent-1", "system_interface", "TimeoutException")
    stuck = tracker.snapshot_stuck()
    assert len(stuck) == 1
    info = stuck[0]
    assert info.agent_id == "agent-1"
    assert info.server_name == "system_interface"
    assert info.failure_count >= 3
    assert info.last_error_class == "TimeoutException"


def test_success_after_stuck_clears_state() -> None:
    tracker = WorkspaceServerHealthTracker(failure_threshold=2)
    for _ in range(2):
        tracker.record_failure("agent-1", "web", "TimeoutException")
    assert len(tracker.snapshot_stuck()) == 1

    tracker.record_success("agent-1", "web")
    assert tracker.snapshot_stuck() == ()


def test_failures_outside_window_are_not_counted() -> None:
    """Old failures age out, preventing spurious "stuck" signals from long-quiet history."""
    now = [1000.0]
    tracker = WorkspaceServerHealthTracker(window_seconds=30.0, failure_threshold=3)
    tracker.set_clock(lambda: now[0])

    for _ in range(2):
        tracker.record_failure("agent-1", "system_interface", "TimeoutException")
    # Advance well past the 30s window so the first two failures age out.
    now[0] += 60.0
    tracker.record_failure("agent-1", "system_interface", "TimeoutException")

    # Only the most recent failure should be within the window.
    assert tracker.snapshot_stuck() == ()


def test_failures_for_different_keys_do_not_accumulate_together() -> None:
    """Failures for (agent-1, web) should not push (agent-1, system_interface) toward stuck."""
    tracker = WorkspaceServerHealthTracker(failure_threshold=3)
    for _ in range(3):
        tracker.record_failure("agent-1", "web", "TimeoutException")
    stuck = tracker.snapshot_stuck()
    assert len(stuck) == 1
    assert stuck[0].server_name == "web"


def test_callback_fires_on_stuck_transition() -> None:
    """Callback fires once when the healthy -> stuck transition happens, not on every failure."""
    tracker = WorkspaceServerHealthTracker(failure_threshold=2)
    call_count = [0]

    def on_change() -> None:
        call_count[0] += 1

    tracker.add_on_change_callback(on_change)

    # Below threshold: no transition yet.
    tracker.record_failure("a", "s", "TimeoutException")
    assert call_count[0] == 0

    # Threshold crossed: callback fires exactly once.
    tracker.record_failure("a", "s", "TimeoutException")
    assert call_count[0] == 1

    # Already stuck: additional failures do not fire again.
    tracker.record_failure("a", "s", "TimeoutException")
    assert call_count[0] == 1


def test_callback_fires_on_recovery_transition() -> None:
    """Callback fires once when a stuck server recovers, not on subsequent successes."""
    tracker = WorkspaceServerHealthTracker(failure_threshold=2)
    call_count = [0]

    def on_change() -> None:
        call_count[0] += 1

    for _ in range(2):
        tracker.record_failure("a", "s", "TimeoutException")
    tracker.add_on_change_callback(on_change)

    # Stuck -> recovered fires exactly once.
    tracker.record_success("a", "s")
    assert call_count[0] == 1

    # Already recovered: no further callback.
    tracker.record_success("a", "s")
    assert call_count[0] == 1


def test_remove_callback_is_silent_for_unknown_callable() -> None:
    """Removing a callback that was never registered is a no-op, not an error."""
    tracker = WorkspaceServerHealthTracker()
    tracker.remove_on_change_callback(lambda: None)
