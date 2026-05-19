import time
from collections.abc import Callable

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import filter_one_host
from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentOrHostAddress
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr_wait.data_types import CombinedState
from imbue.mngr_wait.data_types import StateChange
from imbue.mngr_wait.data_types import WaitResult
from imbue.mngr_wait.data_types import WaitTarget
from imbue.mngr_wait.data_types import check_state_match
from imbue.mngr_wait.primitives import WaitTargetType


class ResolvedTarget(FrozenModel):
    """Resolved wait target with provider and host references for polling."""

    model_config = {"arbitrary_types_allowed": True}

    target: WaitTarget = Field(description="The wait target identity")
    provider: BaseProviderInstance = Field(description="Provider instance for host access")
    host_id: HostId = Field(description="Host ID to poll")
    agent_id: AgentId | None = Field(default=None, description="Agent ID to poll, if agent target")


def resolve_wait_target(
    address: AgentOrHostAddress,
    mngr_ctx: MngrContext,
) -> ResolvedTarget:
    """Resolve an :class:`AgentOrHostAddress` to a :class:`ResolvedTarget`.

    Agent vs host is decided by the address type (no state-based fallback).
    Raises :class:`UserInputError` if the target cannot be found.
    """
    if isinstance(address, AgentAddress):
        return _build_agent_resolved_target(address, mngr_ctx)
    return _build_host_resolved_target(address, mngr_ctx)


def _build_agent_resolved_target(address: AgentAddress, mngr_ctx: MngrContext) -> ResolvedTarget:
    host_ref, agent_ref = find_one_agent(address, mngr_ctx)
    provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
    return ResolvedTarget(
        target=WaitTarget(identifier=str(address), target_type=WaitTargetType.AGENT),
        provider=provider,
        host_id=host_ref.host_id,
        agent_id=agent_ref.agent_id,
    )


def _build_host_resolved_target(address: HostAddress, mngr_ctx: MngrContext) -> ResolvedTarget:
    agents_by_host, _ = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=None,
        agent_identifiers=None,
        include_destroyed=False,
        reset_caches=False,
    )
    all_hosts = list(agents_by_host.keys())
    host_ref = filter_one_host(address, all_hosts)
    provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
    return ResolvedTarget(
        target=WaitTarget(identifier=str(address), target_type=WaitTargetType.HOST),
        provider=provider,
        host_id=host_ref.host_id,
        agent_id=None,
    )


def poll_target_state(
    resolved: ResolvedTarget,
) -> CombinedState:
    """Poll the current state of the resolved target.

    Gets a fresh host interface from the provider and queries state directly.
    Does NOT call reset_caches().

    When any operation fails with a HostConnectionError (e.g. SSH unreachable
    because the host was destroyed), falls back to the offline host
    representation to determine the provider-level state (DESTROYED, STOPPED, etc.).
    """
    try:
        host_interface = resolved.provider.get_host(resolved.host_id)
        host_state = host_interface.get_state()

        agent_state: AgentLifecycleState | None = None
        if resolved.agent_id is not None:
            if isinstance(host_interface, OnlineHostInterface):
                agent_state = _get_agent_lifecycle_state(host_interface, resolved.agent_id)
            else:
                agent_state = AgentLifecycleState.STOPPED

        return CombinedState(host_state=host_state, agent_state=agent_state)
    except HostConnectionError as exc:
        # Host is unreachable (e.g. destroyed, stopped) -- get state from provider metadata
        logger.debug("Host unreachable, falling back to offline state: {}", exc)
        offline_host = resolved.provider.to_offline_host(resolved.host_id)
        offline_agent_state = AgentLifecycleState.STOPPED if resolved.agent_id is not None else None
        return CombinedState(host_state=offline_host.get_state(), agent_state=offline_agent_state)


def _get_agent_lifecycle_state(
    host: OnlineHostInterface,
    agent_id: AgentId,
) -> AgentLifecycleState:
    """Get the lifecycle state of a specific agent on an online host."""
    for agent in host.get_agents():
        if agent.id == agent_id:
            return agent.get_lifecycle_state()
    # Agent not found on host -- treat as stopped
    logger.warning("Agent {} not found on host {}, treating as STOPPED", agent_id, host.id)
    return AgentLifecycleState.STOPPED


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
