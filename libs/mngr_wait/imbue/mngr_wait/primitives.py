from enum import auto
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import HostState

# Exit codes
EXIT_CODE_SUCCESS: Final[int] = 0
EXIT_CODE_ERROR: Final[int] = 1
EXIT_CODE_TIMEOUT: Final[int] = 2


class WaitTargetType(UpperCaseStrEnum):
    """Whether the wait target is an agent or a host."""

    AGENT = auto()
    HOST = auto()


# Terminal states for agents (states where the agent is no longer actively running)
TERMINAL_AGENT_STATES: Final[frozenset[AgentLifecycleState]] = frozenset(
    {
        AgentLifecycleState.STOPPED,
        AgentLifecycleState.WAITING,
        AgentLifecycleState.REPLACED,
        AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE,
        AgentLifecycleState.DONE,
    }
)

# Terminal states for hosts (states where the host is no longer actively running)
TERMINAL_HOST_STATES: Final[frozenset[HostState]] = frozenset(
    {
        HostState.STOPPED,
        HostState.PAUSED,
        HostState.CRASHED,
        HostState.FAILED,
        HostState.DESTROYED,
        HostState.UNAUTHENTICATED,
    }
)

# All valid state strings (union of both enums, uppercased)
ALL_VALID_STATE_STRINGS: Final[frozenset[str]] = frozenset(
    {s.value for s in AgentLifecycleState} | {s.value for s in HostState}
)

# States that exist in both enums
SHARED_STATE_STOPPED: Final[str] = "STOPPED"
SHARED_STATE_RUNNING: Final[str] = "RUNNING"
