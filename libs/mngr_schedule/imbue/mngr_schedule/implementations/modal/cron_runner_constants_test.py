from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr_schedule.data_types import VerifyMode
from imbue.mngr_schedule.implementations.modal.cron_runner_constants import AGENT_MISSING_STATE
from imbue.mngr_schedule.implementations.modal.cron_runner_constants import RUNNING_STATES
from imbue.mngr_schedule.implementations.modal.cron_runner_constants import VALID_VERIFY_MODES


def test_running_states_match_agent_lifecycle_state_enum() -> None:
    expected = {
        AgentLifecycleState.RUNNING.value,
        AgentLifecycleState.WAITING.value,
        AgentLifecycleState.REPLACED.value,
        AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE.value,
    }
    assert RUNNING_STATES == frozenset(expected)


def test_agent_lifecycle_state_enum_has_expected_closed_set() -> None:
    # Pins the full enum so that any addition or removal forces a reviewer
    # to reconcile both sides of the mirror (RUNNING_STATES in the sibling
    # module, _TERMINAL_SUCCESS_STATES in verification.py).
    assert {state.value for state in AgentLifecycleState} == {
        "RUNNING",
        "WAITING",
        "REPLACED",
        "RUNNING_UNKNOWN_AGENT_TYPE",
        "STOPPED",
        "DONE",
    }


def test_valid_verify_modes_match_verify_mode_enum() -> None:
    assert VALID_VERIFY_MODES == frozenset(mode.value.lower() for mode in VerifyMode)


def test_agent_missing_state_is_not_a_real_lifecycle_state() -> None:
    # The sentinel must be disjoint from real lifecycle states so the
    # deploy-side verifier can reliably tell them apart.
    assert AGENT_MISSING_STATE not in {state.value for state in AgentLifecycleState}
