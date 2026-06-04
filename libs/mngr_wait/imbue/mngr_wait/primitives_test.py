from inline_snapshot import snapshot

from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import HostState
from imbue.mngr_wait.primitives import ALL_VALID_STATE_STRINGS
from imbue.mngr_wait.primitives import TERMINAL_AGENT_STATES
from imbue.mngr_wait.primitives import TERMINAL_HOST_STATES


def test_terminal_agent_states_match_exact_expected_set() -> None:
    # Pin the exact terminal agent set: this drives the default-wait semantics,
    # so adding or removing a terminal state must be an intentional change.
    assert frozenset(state.value for state in TERMINAL_AGENT_STATES) == snapshot(
        {"STOPPED", "WAITING", "REPLACED", "RUNNING_UNKNOWN_AGENT_TYPE", "DONE"}
    )


def test_terminal_host_states_match_exact_expected_set() -> None:
    assert frozenset(state.value for state in TERMINAL_HOST_STATES) == snapshot(
        {"STOPPED", "PAUSED", "CRASHED", "FAILED", "DESTROYED", "UNAUTHENTICATED"}
    )


def test_all_valid_state_strings_match_exact_expected_set() -> None:
    assert ALL_VALID_STATE_STRINGS == snapshot(
        {
            "BUILDING",
            "STARTING",
            "RUNNING",
            "STOPPING",
            "STOPPED",
            "PAUSED",
            "CRASHED",
            "FAILED",
            "DESTROYED",
            "UNAUTHENTICATED",
            "UNKNOWN",
            "WAITING",
            "REPLACED",
            "RUNNING_UNKNOWN_AGENT_TYPE",
            "DONE",
        }
    )


def test_terminal_agent_states_does_not_include_running() -> None:
    assert AgentLifecycleState.RUNNING not in TERMINAL_AGENT_STATES


def test_terminal_host_states_does_not_include_running() -> None:
    assert HostState.RUNNING not in TERMINAL_HOST_STATES
