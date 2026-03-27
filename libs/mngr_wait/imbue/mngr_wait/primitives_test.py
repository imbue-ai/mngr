from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import HostState
from imbue.mngr_wait.primitives import ALL_VALID_STATE_STRINGS
from imbue.mngr_wait.primitives import TERMINAL_AGENT_STATES
from imbue.mngr_wait.primitives import TERMINAL_HOST_STATES


def test_terminal_agent_states_are_subset_of_agent_lifecycle_states() -> None:
    for state in TERMINAL_AGENT_STATES:
        assert state in AgentLifecycleState


def test_terminal_host_states_are_subset_of_host_states() -> None:
    for state in TERMINAL_HOST_STATES:
        assert state in HostState


def test_all_valid_state_strings_contains_all_enum_values() -> None:
    for state in AgentLifecycleState:
        assert state.value in ALL_VALID_STATE_STRINGS
    for state in HostState:
        assert state.value in ALL_VALID_STATE_STRINGS


def test_terminal_agent_states_does_not_include_running() -> None:
    assert AgentLifecycleState.RUNNING not in TERMINAL_AGENT_STATES


def test_terminal_host_states_does_not_include_running() -> None:
    assert HostState.RUNNING not in TERMINAL_HOST_STATES
