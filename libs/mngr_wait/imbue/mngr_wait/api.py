import time
from collections.abc import Callable

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.mngr.api.discover import discover_all_hosts_and_agents
from imbue.mngr.api.find import resolve_agent_reference
from imbue.mngr.api.find import resolve_host_reference
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
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
    identifier: str,
    mngr_ctx: MngrContext,
) -> ResolvedTarget:
    """Resolve a target identifier to provider, host, and optional agent references.

    Uses the existing find.py resolution functions for agent/host lookup.
    """
    with log_span("Discovering hosts and agents"):
        agents_by_host, _providers = discover_all_hosts_and_agents(mngr_ctx)

    all_hosts = list(agents_by_host.keys())

    # Determine target type from identifier format
    if identifier.startswith("agent-"):
        return _build_agent_resolved_target(identifier, agents_by_host, mngr_ctx)
    elif identifier.startswith("host-"):
        return _build_host_resolved_target(identifier, all_hosts, mngr_ctx)
    else:
        # Ambiguous name -- try agent first, then host, error on ambiguity
        return _resolve_by_name(identifier, agents_by_host, all_hosts, mngr_ctx)


def _build_agent_resolved_target(
    identifier: str,
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]],
    mngr_ctx: MngrContext,
) -> ResolvedTarget:
    """Build a ResolvedTarget for an agent identifier using find.py's resolve_agent_reference.

    resolve_agent_reference raises UserInputError if not found; it only returns None
    when identifier is None, which cannot happen here.
    """
    result = resolve_agent_reference(identifier, resolved_host=None, agents_by_host=agents_by_host)
    assert result is not None
    host_ref, agent_ref = result
    provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
    return ResolvedTarget(
        target=WaitTarget(identifier=identifier, target_type=WaitTargetType.AGENT),
        provider=provider,
        host_id=host_ref.host_id,
        agent_id=agent_ref.agent_id,
    )


def _build_host_resolved_target(
    identifier: str,
    all_hosts: list[DiscoveredHost],
    mngr_ctx: MngrContext,
) -> ResolvedTarget:
    """Build a ResolvedTarget for a host identifier using find.py's resolve_host_reference.

    resolve_host_reference raises UserInputError if not found; it only returns None
    when identifier is None, which cannot happen here.
    """
    host_ref = resolve_host_reference(identifier, all_hosts)
    assert host_ref is not None
    provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
    return ResolvedTarget(
        target=WaitTarget(identifier=identifier, target_type=WaitTargetType.HOST),
        provider=provider,
        host_id=host_ref.host_id,
        agent_id=None,
    )


def _resolve_by_name(
    identifier: str,
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]],
    all_hosts: list[DiscoveredHost],
    mngr_ctx: MngrContext,
) -> ResolvedTarget:
    """Resolve a name by trying agent names first, then host names.

    Raises UserInputError if the name matches both an agent and a host.
    """
    # Try agent resolution (suppressing errors to try host next)
    agent_result: tuple[DiscoveredHost, DiscoveredAgent] | None = None
    agent_error: UserInputError | None = None
    try:
        agent_result = resolve_agent_reference(identifier, resolved_host=None, agents_by_host=agents_by_host)
    except UserInputError as exc:
        agent_error = exc

    # Try host resolution
    host_ref: DiscoveredHost | None = None
    host_error: UserInputError | None = None
    try:
        host_ref = resolve_host_reference(identifier, all_hosts)
    except UserInputError as exc:
        host_error = exc

    # If both match, it's ambiguous
    if agent_result is not None and host_ref is not None:
        raise UserInputError(
            f"Name '{identifier}' matches both an agent and a host. "
            "Use the full ID (agent-* or host-*) to disambiguate."
        )

    # If agent matched (possibly with error for multiple matches)
    if agent_result is not None:
        matched_host_ref, agent_ref = agent_result
        provider = get_provider_instance(matched_host_ref.provider_name, mngr_ctx)
        return ResolvedTarget(
            target=WaitTarget(identifier=identifier, target_type=WaitTargetType.AGENT),
            provider=provider,
            host_id=matched_host_ref.host_id,
            agent_id=agent_ref.agent_id,
        )

    # If agent had a "multiple matches" error, re-raise it
    if agent_error is not None and "Multiple" in str(agent_error):
        raise agent_error

    # If host matched
    if host_ref is not None:
        provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
        return ResolvedTarget(
            target=WaitTarget(identifier=identifier, target_type=WaitTargetType.HOST),
            provider=provider,
            host_id=host_ref.host_id,
            agent_id=None,
        )

    # If host had a "multiple matches" error, re-raise it
    if host_error is not None and "Multiple" in str(host_error):
        raise host_error

    raise UserInputError(
        f"No agent or host found with name or ID: '{identifier}'. Use 'mngr list' to see available agents and hosts."
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
