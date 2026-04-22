from collections.abc import Callable
from concurrent.futures import Future
from threading import Lock
from typing import Any

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.logging import log_call
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.config.agent_class_registry import get_agent_class
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.errors import BaseMngrError
from imbue.mngr.errors import HostOfflineError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderInstanceNotFoundError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import InterruptibleAgentMixin
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.utils.cel_utils import apply_cel_filters_to_context
from imbue.mngr.utils.cel_utils import compile_cel_filters

_NOT_INTERRUPTIBLE_REASON = "Agent type does not support interrupt"


def agent_type_supports_interrupt(agent_type: str | None) -> bool:
    """Return True if the class registered for this agent type implements :class:`InterruptibleAgentMixin`.

    Returns False for unknown types (the registry's default fallback class is
    not interruptible) and for None. Requires that agent plugins have already
    been loaded (``load_agents_from_plugins``).
    """
    if agent_type is None:
        return False
    try:
        cls = get_agent_class(agent_type)
    except MngrError:
        return False
    return issubclass(cls, InterruptibleAgentMixin)


class InterruptResult(MutableModel):
    """Result of sending interrupt signals to agents."""

    successful_agents: list[str] = Field(default_factory=list, description="List of agent names that were interrupted")
    failed_agents: list[tuple[str, str]] = Field(
        default_factory=list, description="List of (agent_name, error_message) tuples"
    )


@log_call
def interrupt_agents(
    mngr_ctx: MngrContext,
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
    all_agents: bool = False,
    error_behavior: ErrorBehavior = ErrorBehavior.CONTINUE,
    on_success: Callable[[str], None] | None = None,
    on_error: Callable[[str, str], None] | None = None,
    provider_names: tuple[str, ...] | None = None,
) -> InterruptResult:
    """Interrupt the current turn of agents matching the specified criteria.

    Agents whose type does not implement :class:`InterruptibleAgentMixin` are
    reported in ``failed_agents``. Hosts are resolved and interrupts are sent
    concurrently so a slow host does not block others.
    """
    result = InterruptResult()
    result_lock = Lock()

    compiled_include_filters: list[Any] = []
    compiled_exclude_filters: list[Any] = []
    if include_filters or exclude_filters:
        with log_span("Compiling CEL filters", include_filters=include_filters, exclude_filters=exclude_filters):
            compiled_include_filters, compiled_exclude_filters = compile_cel_filters(include_filters, exclude_filters)

    with log_span("Loading agents from all providers"):
        agents_by_host, providers = discover_hosts_and_agents(
            mngr_ctx,
            provider_names=provider_names,
            agent_identifiers=None,
            include_destroyed=False,
            reset_caches=False,
        )
    provider_map = {provider.name: provider for provider in providers}
    logger.trace("Found {} hosts with agents", len(agents_by_host))

    futures: list[Future[None]] = []
    with ConcurrencyGroupExecutor(
        parent_cg=mngr_ctx.concurrency_group, name="interrupt_agents", max_workers=32
    ) as executor:
        for host_ref, agent_refs in agents_by_host.items():
            provider = provider_map.get(host_ref.provider_name)
            if not provider:
                exception = ProviderInstanceNotFoundError(host_ref.provider_name)
                if error_behavior == ErrorBehavior.ABORT:
                    raise exception
                logger.warning("Provider not found: {}", host_ref.provider_name)
                continue

            futures.append(
                executor.submit(
                    _process_host_for_interrupt,
                    host_ref=host_ref,
                    agent_refs=agent_refs,
                    provider=provider,
                    compiled_include_filters=compiled_include_filters,
                    compiled_exclude_filters=compiled_exclude_filters,
                    all_agents=all_agents,
                    include_filters=include_filters,
                    error_behavior=error_behavior,
                    result=result,
                    result_lock=result_lock,
                    parent_cg=mngr_ctx.concurrency_group,
                    on_success=on_success,
                    on_error=on_error,
                )
            )

    for future in futures:
        future.result()

    return result


def _process_host_for_interrupt(
    host_ref: DiscoveredHost,
    agent_refs: list[DiscoveredAgent],
    provider: BaseProviderInstance,
    compiled_include_filters: list[Any],
    compiled_exclude_filters: list[Any],
    all_agents: bool,
    include_filters: tuple[str, ...],
    error_behavior: ErrorBehavior,
    result: InterruptResult,
    result_lock: Lock,
    parent_cg: ConcurrencyGroup,
    on_success: Callable[[str], None] | None,
    on_error: Callable[[str, str], None] | None,
) -> None:
    """Resolve a single host, filter its agents, and interrupt concurrently."""
    try:
        host_interface = provider.get_host(host_ref.host_id)

        if not isinstance(host_interface, OnlineHostInterface):
            exception = HostOfflineError(f"Host '{host_ref.host_id}' is offline. Cannot interrupt agents.")
            if error_behavior == ErrorBehavior.ABORT:
                raise exception
            logger.warning("Host is offline: {}", host_ref.host_id)
            for agent_ref in agent_refs:
                with result_lock:
                    result.failed_agents.append((str(agent_ref.agent_name), str(exception)))
                if on_error:
                    on_error(str(agent_ref.agent_name), str(exception))
            return
        host = host_interface

        agents = host.get_agents()
        agents_to_interrupt: list[AgentInterface] = []

        for agent_ref in agent_refs:
            agent = next((a for a in agents if a.id == agent_ref.agent_id), None)

            if agent is None:
                exception = AgentNotFoundOnHostError(agent_ref.agent_id, host_ref.host_id)
                if error_behavior == ErrorBehavior.ABORT:
                    raise exception
                error_msg = str(exception)
                with result_lock:
                    result.failed_agents.append((str(agent_ref.agent_name), error_msg))
                if on_error:
                    on_error(str(agent_ref.agent_name), error_msg)
                continue

            if compiled_include_filters or compiled_exclude_filters or not all_agents:
                agent_context = _agent_to_cel_context(agent, str(host_ref.host_name), host_ref.provider_name)
                is_included = apply_cel_filters_to_context(
                    context=agent_context,
                    include_filters=compiled_include_filters,
                    exclude_filters=compiled_exclude_filters,
                    error_context_description=f"agent {agent.name}",
                )
                if not all_agents and not include_filters and not is_included:
                    continue
                if not is_included:
                    continue

            agents_to_interrupt.append(agent)

        interrupt_futures: list[Future[None]] = []
        with ConcurrencyGroupExecutor(
            parent_cg=parent_cg, name=f"interrupt_{host_ref.host_id}", max_workers=32
        ) as interrupt_executor:
            for agent in agents_to_interrupt:
                interrupt_futures.append(
                    interrupt_executor.submit(
                        _interrupt_single_agent,
                        agent=agent,
                        result=result,
                        result_lock=result_lock,
                        error_behavior=error_behavior,
                        on_success=on_success,
                        on_error=on_error,
                    )
                )

        for future in interrupt_futures:
            future.result()

    except MngrError as e:
        if error_behavior == ErrorBehavior.ABORT:
            raise
        logger.warning("Error accessing host {}: {}", host_ref.host_id, e)


def _interrupt_single_agent(
    agent: AgentInterface,
    result: InterruptResult,
    result_lock: Lock,
    error_behavior: ErrorBehavior,
    on_success: Callable[[str], None] | None,
    on_error: Callable[[str, str], None] | None,
) -> None:
    """Interrupt a single agent, handling non-interruptible agents and failures."""
    agent_name = str(agent.name)

    if not isinstance(agent, InterruptibleAgentMixin):
        with result_lock:
            result.failed_agents.append((agent_name, _NOT_INTERRUPTIBLE_REASON))
        if on_error:
            on_error(agent_name, _NOT_INTERRUPTIBLE_REASON)
        if error_behavior == ErrorBehavior.ABORT:
            raise MngrError(f"Cannot interrupt {agent_name}: {_NOT_INTERRUPTIBLE_REASON}")
        return

    try:
        with log_span("Interrupting agent {}", agent_name):
            agent.interrupt_current_turn()
        with result_lock:
            result.successful_agents.append(agent_name)
        if on_success:
            on_success(agent_name)
    except BaseMngrError as e:
        error_msg = str(e)
        with result_lock:
            result.failed_agents.append((agent_name, error_msg))
        if on_error:
            on_error(agent_name, error_msg)
        if error_behavior == ErrorBehavior.ABORT:
            raise MngrError(error_msg) from e


def _agent_to_cel_context(agent: AgentInterface, host_name: str, provider_name: str) -> dict[str, Any]:
    """Convert an agent to a CEL-friendly dict for filtering."""
    return {
        "id": str(agent.id),
        "name": str(agent.name),
        "type": str(agent.agent_type),
        "state": agent.get_lifecycle_state().value,
        "host": {
            "id": str(agent.host_id),
            "name": host_name,
            "provider": provider_name,
        },
    }
