from imbue.mngr.api.agent_state import CombinedState
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import HostState
from imbue.mngr_wait.api import _detect_state_changes
from imbue.mngr_wait.api import wait_for_state
from imbue.mngr_wait.data_types import StateChange
from imbue.mngr_wait.data_types import WaitTarget
from imbue.mngr_wait.primitives import WaitTargetType

# === _detect_state_changes ===


def test_detect_state_changes_records_host_state_change() -> None:
    previous = CombinedState(host_state=HostState.RUNNING)
    current = CombinedState(host_state=HostState.STOPPED)
    changes: list[StateChange] = []
    recorded: list[StateChange] = []

    _detect_state_changes(
        previous_state=previous,
        current_state=current,
        elapsed=5.0,
        state_changes=changes,
        on_state_change=recorded.append,
    )

    assert len(changes) == 1
    assert changes[0].field == "host_state"
    assert changes[0].old_value == "RUNNING"
    assert changes[0].new_value == "STOPPED"
    assert len(recorded) == 1


def test_detect_state_changes_records_agent_state_change() -> None:
    previous = CombinedState(
        host_state=HostState.RUNNING,
        agent_state=AgentLifecycleState.RUNNING,
    )
    current = CombinedState(
        host_state=HostState.RUNNING,
        agent_state=AgentLifecycleState.WAITING,
    )
    changes: list[StateChange] = []
    recorded: list[StateChange] = []

    _detect_state_changes(
        previous_state=previous,
        current_state=current,
        elapsed=10.0,
        state_changes=changes,
        on_state_change=recorded.append,
    )

    assert len(changes) == 1
    assert changes[0].field == "agent_state"
    assert changes[0].old_value == "RUNNING"
    assert changes[0].new_value == "WAITING"
    assert len(recorded) == 1


def test_detect_state_changes_no_change_records_nothing() -> None:
    combined_state = CombinedState(
        host_state=HostState.RUNNING,
        agent_state=AgentLifecycleState.RUNNING,
    )
    changes: list[StateChange] = []

    _detect_state_changes(
        previous_state=combined_state,
        current_state=combined_state,
        elapsed=5.0,
        state_changes=changes,
        on_state_change=None,
    )

    assert len(changes) == 0


def test_detect_state_changes_both_change() -> None:
    previous = CombinedState(
        host_state=HostState.RUNNING,
        agent_state=AgentLifecycleState.RUNNING,
    )
    current = CombinedState(
        host_state=HostState.STOPPED,
        agent_state=AgentLifecycleState.STOPPED,
    )
    changes: list[StateChange] = []

    _detect_state_changes(
        previous_state=previous,
        current_state=current,
        elapsed=15.0,
        state_changes=changes,
        on_state_change=None,
    )

    assert len(changes) == 2
    assert changes[0].field == "host_state"
    assert changes[1].field == "agent_state"


def test_detect_state_changes_skips_none_previous() -> None:
    previous = CombinedState()
    current = CombinedState(host_state=HostState.RUNNING)
    changes: list[StateChange] = []

    _detect_state_changes(
        previous_state=previous,
        current_state=current,
        elapsed=1.0,
        state_changes=changes,
        on_state_change=None,
    )

    # No change recorded because previous was None
    assert len(changes) == 0


# === wait_for_state ===


def _make_wait_target(target_type: WaitTargetType = WaitTargetType.HOST) -> WaitTarget:
    return WaitTarget(identifier="test-target", target_type=target_type)


def test_wait_for_state_returns_immediately_when_already_matched() -> None:
    target = _make_wait_target(WaitTargetType.HOST)
    combined_state = CombinedState(host_state=HostState.STOPPED)

    result = wait_for_state(
        target=target,
        poll_fn=lambda: combined_state,
        target_states=frozenset({"STOPPED"}),
        timeout_seconds=5.0,
        interval_seconds=1.0,
        on_state_change=None,
    )

    assert result.is_matched is True
    assert result.is_timed_out is False
    assert result.matched_state == "STOPPED"
    assert result.elapsed_seconds < 1.0


def test_wait_for_state_times_out_when_state_never_matches() -> None:
    target = _make_wait_target(WaitTargetType.HOST)
    combined_state = CombinedState(host_state=HostState.RUNNING)

    result = wait_for_state(
        target=target,
        poll_fn=lambda: combined_state,
        target_states=frozenset({"STOPPED"}),
        timeout_seconds=0.1,
        interval_seconds=0.05,
        on_state_change=None,
    )

    assert result.is_matched is False
    assert result.is_timed_out is True
    assert result.matched_state is None


def test_wait_for_state_detects_state_transition() -> None:
    target = _make_wait_target(WaitTargetType.HOST)
    call_count = 0

    def _poll_with_transition() -> CombinedState:
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            return CombinedState(host_state=HostState.STOPPED)
        return CombinedState(host_state=HostState.RUNNING)

    result = wait_for_state(
        target=target,
        poll_fn=_poll_with_transition,
        target_states=frozenset({"STOPPED"}),
        timeout_seconds=5.0,
        interval_seconds=0.01,
        on_state_change=None,
    )

    assert result.is_matched is True
    assert result.matched_state == "STOPPED"
    # Exactly 1 state change: RUNNING -> STOPPED
    assert len(result.state_changes) == 1


def test_wait_for_state_records_state_changes_through_multiple_transitions() -> None:
    target = _make_wait_target(WaitTargetType.HOST)
    call_count = 0

    def _poll_through_states() -> CombinedState:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return CombinedState(host_state=HostState.RUNNING)
        elif call_count == 2:
            return CombinedState(host_state=HostState.STOPPING)
        else:
            return CombinedState(host_state=HostState.STOPPED)

    recorded_changes: list[StateChange] = []

    result = wait_for_state(
        target=target,
        poll_fn=_poll_through_states,
        target_states=frozenset({"STOPPED"}),
        timeout_seconds=5.0,
        interval_seconds=0.01,
        on_state_change=recorded_changes.append,
    )

    assert result.is_matched is True
    # Exactly 2 changes: RUNNING -> STOPPING, STOPPING -> STOPPED
    assert len(result.state_changes) == 2
    assert len(recorded_changes) == 2


def test_wait_for_state_handles_poll_errors_gracefully() -> None:
    target = _make_wait_target(WaitTargetType.HOST)
    call_count = 0

    def _poll_with_error() -> CombinedState:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("transient error")
        return CombinedState(host_state=HostState.STOPPED)

    result = wait_for_state(
        target=target,
        poll_fn=_poll_with_error,
        target_states=frozenset({"STOPPED"}),
        timeout_seconds=5.0,
        interval_seconds=0.01,
        on_state_change=None,
    )

    assert result.is_matched is True
    assert result.matched_state == "STOPPED"


def test_wait_for_state_agent_target_matches_agent_state() -> None:
    target = _make_wait_target(WaitTargetType.AGENT)
    combined_state = CombinedState(
        host_state=HostState.RUNNING,
        agent_state=AgentLifecycleState.WAITING,
    )

    result = wait_for_state(
        target=target,
        poll_fn=lambda: combined_state,
        target_states=frozenset({"WAITING"}),
        timeout_seconds=5.0,
        interval_seconds=1.0,
        on_state_change=None,
    )

    assert result.is_matched is True
    assert result.matched_state == "WAITING"


def test_wait_for_state_agent_target_matches_host_crashed() -> None:
    target = _make_wait_target(WaitTargetType.AGENT)
    combined_state = CombinedState(
        host_state=HostState.CRASHED,
        agent_state=AgentLifecycleState.RUNNING,
    )

    result = wait_for_state(
        target=target,
        poll_fn=lambda: combined_state,
        target_states=frozenset({"CRASHED"}),
        timeout_seconds=5.0,
        interval_seconds=1.0,
        on_state_change=None,
    )

    assert result.is_matched is True
    assert result.matched_state == "CRASHED"


def test_wait_for_state_records_running_to_destroyed_transition_from_state_sequence() -> None:
    """wait_for_state records a RUNNING -> DESTROYED change given that state sequence.

    This exercises wait_for_state's change-recording and matching against a
    hand-fed sequence; it does NOT exercise poll_combined_state's real
    HostConnectionError fallback (that is covered by the poll_combined_state
    tests in the core api.agent_state tests).
    """
    target = _make_wait_target(WaitTargetType.HOST)
    call_count = 0

    def _poll_with_destruction() -> CombinedState:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            # First two polls: host is running
            return CombinedState(host_state=HostState.RUNNING)
        else:
            # After destruction: offline host reports DESTROYED
            return CombinedState(host_state=HostState.DESTROYED)

    result = wait_for_state(
        target=target,
        poll_fn=_poll_with_destruction,
        target_states=frozenset({"DESTROYED", "STOPPED", "CRASHED"}),
        timeout_seconds=5.0,
        interval_seconds=0.01,
        on_state_change=None,
    )

    assert result.is_matched is True
    assert result.matched_state == "DESTROYED"
    assert len(result.state_changes) == 1
    assert result.state_changes[0].old_value == "RUNNING"
    assert result.state_changes[0].new_value == "DESTROYED"
