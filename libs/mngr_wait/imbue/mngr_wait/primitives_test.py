from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import HostState
from imbue.mngr_wait.primitives import TERMINAL_AGENT_STATES
from imbue.mngr_wait.primitives import TERMINAL_HOST_STATES


def test_terminal_agent_states_does_not_include_running() -> None:
    assert AgentLifecycleState.RUNNING not in TERMINAL_AGENT_STATES


def test_terminal_host_states_does_not_include_running() -> None:
    assert HostState.RUNNING not in TERMINAL_HOST_STATES
