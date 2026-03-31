import pytest

from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import HostState
from imbue.mngr_wait.data_types import CombinedState
from imbue.mngr_wait.data_types import check_state_match
from imbue.mngr_wait.data_types import compute_default_target_states
from imbue.mngr_wait.data_types import describe_combined_state
from imbue.mngr_wait.data_types import validate_state_strings
from imbue.mngr_wait.primitives import ALL_VALID_STATE_STRINGS
from imbue.mngr_wait.primitives import WaitTargetType

# === compute_default_target_states ===


def test_default_agent_states_include_agent_terminal_states() -> None:
    states = compute_default_target_states(WaitTargetType.AGENT)
    assert "STOPPED" in states
    assert "WAITING" in states
    assert "REPLACED" in states
    assert "RUNNING_UNKNOWN_AGENT_TYPE" in states
    assert "DONE" in states


def test_default_agent_states_include_host_terminal_states() -> None:
    states = compute_default_target_states(WaitTargetType.AGENT)
    assert "CRASHED" in states
    assert "FAILED" in states
    assert "DESTROYED" in states
    assert "UNAUTHENTICATED" in states
    assert "PAUSED" in states


def test_default_agent_states_exclude_running() -> None:
    states = compute_default_target_states(WaitTargetType.AGENT)
    assert "RUNNING" not in states


def test_default_host_states_include_terminal_states() -> None:
    states = compute_default_target_states(WaitTargetType.HOST)
    assert "STOPPED" in states
    assert "CRASHED" in states
    assert "FAILED" in states
    assert "DESTROYED" in states
    assert "UNAUTHENTICATED" in states
    assert "PAUSED" in states


def test_default_host_states_exclude_running_and_transient() -> None:
    states = compute_default_target_states(WaitTargetType.HOST)
    assert "RUNNING" not in states
    assert "BUILDING" not in states
    assert "STARTING" not in states
    assert "STOPPING" not in states


# === describe_combined_state ===


def test_describe_combined_state_host_target_shows_host_state() -> None:
    combined_state = CombinedState(host_state=HostState.RUNNING)
    result = describe_combined_state(combined_state, WaitTargetType.HOST)
    assert result == "RUNNING"


def test_describe_combined_state_host_target_unknown_when_none() -> None:
    combined_state = CombinedState()
    result = describe_combined_state(combined_state, WaitTargetType.HOST)
    assert result == "UNKNOWN"


def test_describe_combined_state_agent_target_shows_both_states() -> None:
    combined_state = CombinedState(
        host_state=HostState.RUNNING,
        agent_state=AgentLifecycleState.WAITING,
    )
    result = describe_combined_state(combined_state, WaitTargetType.AGENT)
    assert "agent=WAITING" in result
    assert "host=RUNNING" in result


def test_describe_combined_state_agent_target_unknown_when_none() -> None:
    combined_state = CombinedState()
    result = describe_combined_state(combined_state, WaitTargetType.AGENT)
    assert result == "UNKNOWN"


def test_describe_combined_state_agent_target_agent_only() -> None:
    combined_state = CombinedState(agent_state=AgentLifecycleState.RUNNING)
    result = describe_combined_state(combined_state, WaitTargetType.AGENT)
    assert "agent=RUNNING" in result
    assert "host=" not in result


def test_describe_combined_state_agent_target_host_only() -> None:
    combined_state = CombinedState(host_state=HostState.CRASHED)
    result = describe_combined_state(combined_state, WaitTargetType.AGENT)
    assert "host=CRASHED" in result
    assert "agent=" not in result


def test_describe_combined_state_host_target_stopped() -> None:
    combined_state = CombinedState(host_state=HostState.STOPPED)
    result = describe_combined_state(combined_state, WaitTargetType.HOST)
    assert result == "STOPPED"


# === check_state_match ===


@pytest.mark.parametrize(
    "host_state, target_states, expected",
    [
        pytest.param(HostState.STOPPED, {"STOPPED"}, "STOPPED", id="host_stopped_matches"),
        pytest.param(HostState.RUNNING, {"STOPPED"}, None, id="host_running_does_not_match_stopped"),
        pytest.param(None, {"STOPPED"}, None, id="none_host_state_does_not_match"),
        pytest.param(HostState.CRASHED, {"CRASHED", "FAILED"}, "CRASHED", id="host_crashed_matches"),
    ],
)
def test_check_state_match_host_target(
    host_state: HostState | None,
    target_states: set[str],
    expected: str | None,
) -> None:
    combined_state = CombinedState(host_state=host_state)
    result = check_state_match(combined_state, WaitTargetType.HOST, frozenset(target_states))
    assert result == expected


@pytest.mark.parametrize(
    "host_state, agent_state, target_states, expected",
    [
        pytest.param(
            HostState.RUNNING,
            AgentLifecycleState.DONE,
            {"DONE"},
            "DONE",
            id="agent_done_matches",
        ),
        pytest.param(
            HostState.RUNNING,
            AgentLifecycleState.WAITING,
            {"WAITING"},
            "WAITING",
            id="agent_waiting_matches",
        ),
        pytest.param(
            HostState.RUNNING,
            AgentLifecycleState.RUNNING,
            {"RUNNING"},
            "RUNNING",
            id="agent_running_matches_running",
        ),
        pytest.param(
            HostState.RUNNING,
            AgentLifecycleState.STOPPED,
            {"RUNNING"},
            None,
            id="agent_stopped_does_not_match_running",
        ),
        pytest.param(
            HostState.RUNNING,
            AgentLifecycleState.WAITING,
            {"RUNNING"},
            None,
            id="host_running_does_not_match_running_for_agent",
        ),
        pytest.param(
            HostState.RUNNING,
            AgentLifecycleState.STOPPED,
            {"STOPPED"},
            "STOPPED",
            id="agent_stopped_matches_stopped",
        ),
        pytest.param(
            HostState.STOPPED,
            AgentLifecycleState.RUNNING,
            {"STOPPED"},
            "STOPPED",
            id="host_stopped_matches_stopped_for_agent",
        ),
        pytest.param(
            HostState.CRASHED,
            AgentLifecycleState.RUNNING,
            {"CRASHED"},
            "CRASHED",
            id="host_crashed_matches_for_agent",
        ),
        pytest.param(
            HostState.PAUSED,
            AgentLifecycleState.RUNNING,
            {"PAUSED"},
            "PAUSED",
            id="host_paused_matches_for_agent",
        ),
        pytest.param(
            HostState.RUNNING,
            AgentLifecycleState.RUNNING,
            {"DONE", "WAITING"},
            None,
            id="no_match_returns_none",
        ),
    ],
)
def test_check_state_match_agent_target(
    host_state: HostState,
    agent_state: AgentLifecycleState,
    target_states: set[str],
    expected: str | None,
) -> None:
    combined_state = CombinedState(host_state=host_state, agent_state=agent_state)
    result = check_state_match(combined_state, WaitTargetType.AGENT, frozenset(target_states))
    assert result == expected


# === validate_state_strings ===


def test_validate_state_strings_accepts_valid_states() -> None:
    result = validate_state_strings(["STOPPED", "running", "Done"], ALL_VALID_STATE_STRINGS)
    assert result == frozenset({"STOPPED", "RUNNING", "DONE"})


def test_validate_state_strings_rejects_invalid_state() -> None:
    with pytest.raises(UserInputError, match="Invalid state"):
        validate_state_strings(["NONEXISTENT"], ALL_VALID_STATE_STRINGS)


def test_validate_state_strings_empty_input() -> None:
    result = validate_state_strings([], ALL_VALID_STATE_STRINGS)
    assert result == frozenset()
