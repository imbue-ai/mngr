import time
from collections.abc import Callable

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.mng.api.discover import discover_all_hosts_and_agents
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import AgentNotFoundError
from imbue.mng.errors import HostNotFoundError
from imbue.mng.errors import UserInputError
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import DiscoveredAgent
from imbue.mng.primitives import DiscoveredHost
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.providers.base_provider import BaseProviderInstance
from imbue.mng_wait.data_types import StateChange
from imbue.mng_wait.data_types import StateSnapshot
from imbue.mng_wait.data_types import WaitResult
from imbue.mng_wait.data_types import WaitTarget
from imbue.mng_wait.data_types import check_state_match
from imbue.mng_wait.primitives import WaitTargetType


class ResolvedTarget(FrozenModel):
    """Resolved wait target with provider and host references for polling."""

    model_config = {"arbitrary_types_allowed": True}

    target: WaitTarget = Field(description="The wait target identity")
    provider: BaseProviderInstance = Field(description="Provider instance for host access")
    host_id: HostId = Field(description="Host ID to poll")
    agent_id: AgentId | None = Field(default=None, description="Agent ID to poll, if agent target")


def resolve_wait_target(
    identifier: str,
    mng_ctx: MngContext,
) -> ResolvedTarget:
    """Resolve a target identifier to provider, host, and optional agent references."""
    with log_span("Discovering hosts and agents"):
        agents_by_host, _providers = discover_all_hosts_and_agents(mng_ctx)

    # Determine target type from identifier format
    if identifier.startswith("agent-"):
        return _resolve_agent_target(identifier, agents_by_host, mng_ctx)
    elif identifier.startswith("host-"):
        return _resolve_host_target(identifier, agents_by_host, mng_ctx)
    else:
        # Ambiguous name -- try agent first, then host
        return _resolve_by_name(identifier, agents_by_host, mng_ctx)


def _is_agent_match(
    agent_ref: DiscoveredAgent,
    identifier: str,
    is_agent_id: bool,
) -> bool:
    """Check if a discovered agent matches the given identifier."""
    if is_agent_id:
        try:
            agent_id = AgentId(identifier)
        except ValueError:
            return False
        return agent_ref.agent_id == agent_id
    else:
        return str(agent_ref.agent_name) == identifier


def _resolve_agent_target(
    identifier: str,
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]],
    mng_ctx: MngContext,
) -> ResolvedTarget:
    """Resolve an agent target by ID or name."""
    is_agent_id = identifier.startswith("agent-")

    matching: list[tuple[DiscoveredHost, DiscoveredAgent]] = []
    for host_ref, agent_refs in agents_by_host.items():
        for agent_ref in agent_refs:
            if _is_agent_match(agent_ref, identifier, is_agent_id):
                matching.append((host_ref, agent_ref))

    if not matching:
        raise AgentNotFoundError(identifier)
    elif len(matching) > 1:
        agent_list = ", ".join(str(a.agent_id) for _, a in matching)
        raise UserInputError(
            f"Multiple agents found with name '{identifier}': {agent_list}. Use the agent ID instead."
        )
    else:
        pass

    host_ref, agent_ref = matching[0]
    provider = get_provider_instance(host_ref.provider_name, mng_ctx)
    return ResolvedTarget(
        target=WaitTarget(identifier=identifier, target_type=WaitTargetType.AGENT),
        provider=provider,
        host_id=host_ref.host_id,
        agent_id=agent_ref.agent_id,
    )


def _is_host_match(
    host_ref: DiscoveredHost,
    identifier: str,
    is_host_id: bool,
) -> bool:
    """Check if a discovered host matches the given identifier."""
    if is_host_id:
        try:
            host_id = HostId(identifier)
        except ValueError:
            return False
        return host_ref.host_id == host_id
    else:
        return str(host_ref.host_name) == identifier


def _resolve_host_target(
    identifier: str,
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]],
    mng_ctx: MngContext,
) -> ResolvedTarget:
    """Resolve a host target by ID or name."""
    is_host_id = identifier.startswith("host-")

    matching: list[DiscoveredHost] = []
    for host_ref in agents_by_host.keys():
        if _is_host_match(host_ref, identifier, is_host_id):
            matching.append(host_ref)

    if not matching:
        raise HostNotFoundError(HostId(identifier) if is_host_id else HostName(identifier))
    elif len(matching) > 1:
        host_list = ", ".join(str(h.host_id) for h in matching)
        raise UserInputError(f"Multiple hosts found with name '{identifier}': {host_list}. Use the host ID instead.")
    else:
        pass

    host_ref = matching[0]
    provider = get_provider_instance(host_ref.provider_name, mng_ctx)
    return ResolvedTarget(
        target=WaitTarget(identifier=identifier, target_type=WaitTargetType.HOST),
        provider=provider,
        host_id=host_ref.host_id,
        agent_id=None,
    )


def _resolve_by_name(
    identifier: str,
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]],
    mng_ctx: MngContext,
) -> ResolvedTarget:
    """Resolve a name by trying agent names first, then host names."""
    # Collect agent matches
    agent_matches: list[tuple[DiscoveredHost, DiscoveredAgent]] = []
    for host_ref, agent_refs in agents_by_host.items():
        for agent_ref in agent_refs:
            if _is_agent_match(agent_ref, identifier, is_agent_id=False):
                agent_matches.append((host_ref, agent_ref))

    # Collect host matches
    host_matches: list[DiscoveredHost] = []
    for host_ref in agents_by_host.keys():
        if _is_host_match(host_ref, identifier, is_host_id=False):
            host_matches.append(host_ref)

    # If both match, it's ambiguous
    if agent_matches and host_matches:
        raise UserInputError(
            f"Name '{identifier}' matches both an agent and a host. "
            "Use the full ID (agent-* or host-*) to disambiguate."
        )

    if agent_matches:
        if len(agent_matches) > 1:
            agent_list = ", ".join(str(a.agent_id) for _, a in agent_matches)
            raise UserInputError(
                f"Multiple agents found with name '{identifier}': {agent_list}. Use the agent ID instead."
            )
        else:
            pass
        host_ref, agent_ref = agent_matches[0]
        provider = get_provider_instance(host_ref.provider_name, mng_ctx)
        return ResolvedTarget(
            target=WaitTarget(identifier=identifier, target_type=WaitTargetType.AGENT),
            provider=provider,
            host_id=host_ref.host_id,
            agent_id=agent_ref.agent_id,
        )
    elif host_matches:
        if len(host_matches) > 1:
            host_list = ", ".join(str(h.host_id) for h in host_matches)
            raise UserInputError(
                f"Multiple hosts found with name '{identifier}': {host_list}. Use the host ID instead."
            )
        else:
            pass
        host_ref = host_matches[0]
        provider = get_provider_instance(host_ref.provider_name, mng_ctx)
        return ResolvedTarget(
            target=WaitTarget(identifier=identifier, target_type=WaitTargetType.HOST),
            provider=provider,
            host_id=host_ref.host_id,
            agent_id=None,
        )
    else:
        raise UserInputError(
            f"No agent or host found with name or ID: '{identifier}'. "
            "Use 'mng list' to see available agents and hosts."
        )


def poll_target_state(
    resolved: ResolvedTarget,
) -> StateSnapshot:
    """Poll the current state of the resolved target.

    Gets a fresh host interface from the provider and queries state directly.
    Does NOT call reset_caches().
    """
    host_interface = resolved.provider.get_host(resolved.host_id)
    host_state = host_interface.get_state()

    agent_state: AgentLifecycleState | None = None
    if resolved.agent_id is not None:
        if isinstance(host_interface, OnlineHostInterface):
            agent_state = _get_agent_lifecycle_state(host_interface, resolved.agent_id)
        else:
            # Host is offline, agent is considered stopped
            agent_state = AgentLifecycleState.STOPPED

    return StateSnapshot(host_state=host_state, agent_state=agent_state)


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
    poll_fn: Callable[[], StateSnapshot],
    target_states: frozenset[str],
    timeout_seconds: float | None,
    interval_seconds: float,
    on_state_change: Callable[[StateChange], None] | None,
) -> WaitResult:
    """Poll until the target reaches one of the target states, or timeout.

    poll_fn is called each iteration to get the current state snapshot.
    """
    start_time = time.monotonic()
    state_changes: list[StateChange] = []
    previous_snapshot = StateSnapshot()
    is_waiting = True

    while is_waiting:
        elapsed = time.monotonic() - start_time

        # Poll current state
        try:
            current_snapshot = poll_fn()
        except Exception as exc:
            logger.warning("Polling error (will retry): {}", exc)
            current_snapshot = StateSnapshot()

        # Detect and log state changes
        _detect_state_changes(
            previous_snapshot=previous_snapshot,
            current_snapshot=current_snapshot,
            elapsed=elapsed,
            state_changes=state_changes,
            on_state_change=on_state_change,
        )
        previous_snapshot = current_snapshot

        # Check for match
        matched_state = check_state_match(
            snapshot=current_snapshot,
            target_type=target.target_type,
            target_states=target_states,
        )
        if matched_state is not None:
            return WaitResult(
                target=target,
                is_matched=True,
                is_timed_out=False,
                final_snapshot=current_snapshot,
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
        final_snapshot=previous_snapshot,
        matched_state=None,
        elapsed_seconds=final_elapsed,
        state_changes=tuple(state_changes),
    )


def _detect_state_changes(
    previous_snapshot: StateSnapshot,
    current_snapshot: StateSnapshot,
    elapsed: float,
    state_changes: list[StateChange],
    on_state_change: Callable[[StateChange], None] | None,
) -> None:
    """Detect and record state changes between two snapshots."""
    if (
        current_snapshot.host_state is not None
        and previous_snapshot.host_state is not None
        and current_snapshot.host_state != previous_snapshot.host_state
    ):
        change = StateChange(
            field="host_state",
            old_value=previous_snapshot.host_state.value,
            new_value=current_snapshot.host_state.value,
            elapsed_seconds=elapsed,
        )
        state_changes.append(change)
        logger.info(
            "Host state changed: {} -> {} (after {:.1f}s)",
            change.old_value,
            change.new_value,
            elapsed,
        )
        if on_state_change is not None:
            on_state_change(change)

    if (
        current_snapshot.agent_state is not None
        and previous_snapshot.agent_state is not None
        and current_snapshot.agent_state != previous_snapshot.agent_state
    ):
        change = StateChange(
            field="agent_state",
            old_value=previous_snapshot.agent_state.value,
            new_value=current_snapshot.agent_state.value,
            elapsed_seconds=elapsed,
        )
        state_changes.append(change)
        logger.info(
            "Agent state changed: {} -> {} (after {:.1f}s)",
            change.old_value,
            change.new_value,
            elapsed,
        )
        if on_state_change is not None:
            on_state_change(change)
