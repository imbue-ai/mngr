from collections.abc import Sequence
from typing import assert_never

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import HostState
from imbue.mngr_wait.primitives import SHARED_STATE_RUNNING
from imbue.mngr_wait.primitives import SHARED_STATE_STOPPED
from imbue.mngr_wait.primitives import TERMINAL_AGENT_STATES
from imbue.mngr_wait.primitives import TERMINAL_HOST_STATES
from imbue.mngr_wait.primitives import WaitTargetType


class WaitTarget(FrozenModel):
    """Identifies what we are waiting on."""

    identifier: str = Field(description="The original identifier string (ID or name)")
    target_type: WaitTargetType = Field(description="Whether this is an agent or host target")


class CombinedState(FrozenModel):
    """Current state of the target at a point in time."""

    host_state: HostState | None = Field(default=None, description="Current host state (None if host is unreachable)")
    agent_state: AgentLifecycleState | None = Field(
        default=None, description="Current agent lifecycle state (None if not an agent target or unreachable)"
    )


class StateChange(FrozenModel):
    """Records a state transition."""

    field: str = Field(description="Which state changed: 'host_state' or 'agent_state'")
    old_value: str = Field(description="Previous state value")
    new_value: str = Field(description="New state value")
    elapsed_seconds: float = Field(description="Seconds since wait started")


class WaitResult(FrozenModel):
    """Result of a wait operation."""

    target: WaitTarget = Field(description="The target that was waited on")
    is_matched: bool = Field(description="Whether the target entered a requested state")
    is_timed_out: bool = Field(description="Whether the wait timed out")
    final_state: CombinedState = Field(description="Final combined state at end of wait")
    matched_state: str | None = Field(default=None, description="The state string that matched, if any")
    elapsed_seconds: float = Field(description="Total seconds spent waiting")
    state_changes: tuple[StateChange, ...] = Field(default=(), description="All state changes observed during wait")


@pure
def describe_combined_state(combined_state: CombinedState, target_type: WaitTargetType) -> str:
    """Return a human-readable description of the current combined state."""
    match target_type:
        case WaitTargetType.HOST:
            if combined_state.host_state is not None:
                return combined_state.host_state.value
            else:
                return "UNKNOWN"
        case WaitTargetType.AGENT:
            parts: list[str] = []
            if combined_state.agent_state is not None:
                parts.append(f"agent={combined_state.agent_state.value}")
            if combined_state.host_state is not None:
                parts.append(f"host={combined_state.host_state.value}")
            if parts:
                return ", ".join(parts)
            else:
                return "UNKNOWN"
        case _ as unreachable:
            assert_never(unreachable)


@pure
def compute_default_target_states(target_type: WaitTargetType) -> frozenset[str]:
    """Return the default set of state strings to wait for, given the target type."""
    match target_type:
        case WaitTargetType.AGENT:
            # Wait for any terminal agent state OR any terminal host state
            agent_strings = frozenset(s.value for s in TERMINAL_AGENT_STATES)
            host_strings = frozenset(s.value for s in TERMINAL_HOST_STATES)
            return agent_strings | host_strings
        case WaitTargetType.HOST:
            return frozenset(s.value for s in TERMINAL_HOST_STATES)
        case _ as unreachable:
            assert_never(unreachable)


@pure
def check_state_match(
    combined_state: CombinedState,
    target_type: WaitTargetType,
    target_states: frozenset[str],
) -> str | None:
    """Check if the current state combined_state matches any of the target states.

    Returns the matched state string, or None if no match.
    Handles the RUNNING/STOPPED overlap rules for agent targets.
    """
    match target_type:
        case WaitTargetType.HOST:
            if combined_state.host_state is not None and combined_state.host_state.value in target_states:
                return combined_state.host_state.value
            return None
        case WaitTargetType.AGENT:
            return _check_agent_state_match(combined_state, target_states)
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _check_agent_state_match(
    combined_state: CombinedState,
    target_states: frozenset[str],
) -> str | None:
    """Check state match for an agent target, handling RUNNING/STOPPED overlap."""
    # Check agent-specific states first
    if combined_state.agent_state is not None:
        agent_state_str = combined_state.agent_state.value
        if agent_state_str == SHARED_STATE_RUNNING:
            # RUNNING only counts if the *agent* is running
            if SHARED_STATE_RUNNING in target_states:
                return SHARED_STATE_RUNNING
        elif agent_state_str == SHARED_STATE_STOPPED:
            # STOPPED counts if agent is stopped
            if SHARED_STATE_STOPPED in target_states:
                return SHARED_STATE_STOPPED
        elif agent_state_str in target_states:
            # Agent-only states (WAITING, DONE, REPLACED)
            return agent_state_str
        else:
            # Agent state not in target states
            pass

    # Check host states (excluding RUNNING, which only counts for agent)
    if combined_state.host_state is not None:
        host_state_str = combined_state.host_state.value
        if host_state_str == SHARED_STATE_RUNNING:
            # Host RUNNING does NOT count when watching an agent
            pass
        elif host_state_str == SHARED_STATE_STOPPED:
            # Host STOPPED counts
            if SHARED_STATE_STOPPED in target_states:
                return SHARED_STATE_STOPPED
        elif host_state_str in target_states:
            # Host-only states (BUILDING, STARTING, STOPPING, PAUSED, CRASHED, etc.)
            return host_state_str
        else:
            # Host state not in target states
            pass

    return None


@pure
def validate_state_strings(
    state_strings: Sequence[str],
    valid_states: frozenset[str],
) -> frozenset[str]:
    """Validate and normalize a sequence of state strings.

    Returns a frozenset of uppercased, validated state strings.
    """
    result: set[str] = set()
    for state_str in state_strings:
        uppercased = state_str.upper()
        if uppercased not in valid_states:
            sorted_valid = sorted(valid_states)
            raise UserInputError(f"Invalid state: '{state_str}'. Valid states: {', '.join(sorted_valid)}")
        result.add(uppercased)
    return frozenset(result)
