import time
from collections.abc import Callable

from loguru import logger

from imbue.mngr.api.agent_state import CombinedState
from imbue.mngr_wait.data_types import StateChange
from imbue.mngr_wait.data_types import WaitResult
from imbue.mngr_wait.data_types import WaitTarget
from imbue.mngr_wait.data_types import check_state_match


def wait_for_state(
    target: WaitTarget,
    poll_fn: Callable[[], CombinedState],
    target_states: frozenset[str],
    timeout_seconds: float | None,
    interval_seconds: float,
    on_state_change: Callable[[StateChange], None] | None,
) -> WaitResult:
    """Poll until the target reaches one of the target states, or timeout.

    poll_fn is called each iteration to get the current combined state.
    """
    start_time = time.monotonic()
    state_changes: list[StateChange] = []
    previous_state = CombinedState()
    is_waiting = True

    while is_waiting:
        elapsed = time.monotonic() - start_time

        # Poll current state
        try:
            current_state = poll_fn()
        except Exception as exc:
            logger.warning("Polling error (will retry): {}", exc)
            current_state = CombinedState()

        # Detect and log state changes
        _detect_state_changes(
            previous_state=previous_state,
            current_state=current_state,
            elapsed=elapsed,
            state_changes=state_changes,
            on_state_change=on_state_change,
        )
        previous_state = current_state

        # Check for match
        matched_state = check_state_match(
            combined_state=current_state,
            target_type=target.target_type,
            target_states=target_states,
        )
        if matched_state is not None:
            return WaitResult(
                target=target,
                is_matched=True,
                is_timed_out=False,
                final_state=current_state,
                matched_state=matched_state,
                elapsed_seconds=time.monotonic() - start_time,
                state_changes=tuple(state_changes),
            )

        # Check timeout
        if timeout_seconds is not None and elapsed >= timeout_seconds:
            is_waiting = False
        else:
            # Sleep for the poll interval
            time.sleep(interval_seconds)

    final_elapsed = time.monotonic() - start_time
    return WaitResult(
        target=target,
        is_matched=False,
        is_timed_out=True,
        final_state=previous_state,
        matched_state=None,
        elapsed_seconds=final_elapsed,
        state_changes=tuple(state_changes),
    )


def _detect_state_changes(
    previous_state: CombinedState,
    current_state: CombinedState,
    elapsed: float,
    state_changes: list[StateChange],
    on_state_change: Callable[[StateChange], None] | None,
) -> None:
    """Detect and record state changes between two combined states."""
    if (
        current_state.host_state is not None
        and previous_state.host_state is not None
        and current_state.host_state != previous_state.host_state
    ):
        change = StateChange(
            field="host_state",
            old_value=previous_state.host_state.value,
            new_value=current_state.host_state.value,
            elapsed_seconds=elapsed,
        )
        state_changes.append(change)
        logger.debug(
            "Host state changed: {} -> {} (after {:.1f}s)",
            change.old_value,
            change.new_value,
            elapsed,
        )
        if on_state_change is not None:
            on_state_change(change)

    if (
        current_state.agent_state is not None
        and previous_state.agent_state is not None
        and current_state.agent_state != previous_state.agent_state
    ):
        change = StateChange(
            field="agent_state",
            old_value=previous_state.agent_state.value,
            new_value=current_state.agent_state.value,
            elapsed_seconds=elapsed,
        )
        state_changes.append(change)
        logger.debug(
            "Agent state changed: {} -> {} (after {:.1f}s)",
            change.old_value,
            change.new_value,
            elapsed,
        )
        if on_state_change is not None:
            on_state_change(change)
