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
from imbue.mng.api.find import ensure_agent_started
from imbue.mng.api.find import ensure_host_started
from imbue.mng.api.list import load_all_agents_grouped_by_host
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import AgentNotFoundOnHostError
from imbue.mng.errors import BaseMngError
from imbue.mng.errors import HostOfflineError
from imbue.mng.errors import MngError
from imbue.mng.errors import ProviderInstanceNotFoundError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentReference
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import HostReference
from imbue.mng.providers.base_provider import BaseProviderInstance
from imbue.mng.utils.cel_utils import apply_cel_filters_to_context
from imbue.mng.utils.cel_utils import compile_cel_filters


class MessageResult(MutableModel):
    """Result of sending messages to agents."""

    successful_agents: list[str] = Field(
        default_factory=list, description="List of agent names that received messages"
    )
    failed_agents: list[tuple[str, str]] = Field(
        default_factory=list, description="List of (agent_name, error_message) tuples"
    )


@log_call
def send_message_to_agents(
    mng_ctx: MngContext,
    message_content: str,
    # CEL expressions - only include agents matching these
    include_filters: tuple[str, ...] = (),
    # CEL expressions - exclude agents matching these
    exclude_filters: tuple[str, ...] = (),
    # If True, send to all agents (filters still apply for exclusion)
    all_agents: bool = False,
    # How to handle errors (abort or continue)
    error_behavior: ErrorBehavior = ErrorBehavior.CONTINUE,
    # If True, automatically start offline hosts and stopped agents before sending
    is_start_desired: bool = False,
    # Optional callback invoked when message is sent successfully
    on_success: Callable[[str], None] | None = None,
    # Optional callback invoked when message fails (agent_name, error)
    on_error: Callable[[str, str], None] | None = None,
) -> MessageResult:
    """Send a message to agents matching the specified criteria.

    Hosts are resolved and messages are sent concurrently so that one slow host
    or one agent's failure does not block messages to other agents.
    """
    result = MessageResult()
    result_lock = Lock()

    # Compile CEL filters if provided
    compiled_include_filters: list[Any] = []
    compiled_exclude_filters: list[Any] = []
    if include_filters or exclude_filters:
        with log_span("Compiling CEL filters", include_filters=include_filters, exclude_filters=exclude_filters):
            compiled_include_filters, compiled_exclude_filters = compile_cel_filters(include_filters, exclude_filters)

    # Load all agents grouped by host
    with log_span("Loading agents from all providers"):
        agents_by_host, providers = load_all_agents_grouped_by_host(mng_ctx)
    provider_map = {provider.name: provider for provider in providers}
    logger.trace("Found {} hosts with agents", len(agents_by_host))

    # Process each host concurrently: resolve host, filter agents, send messages.
    futures: list[Future[None]] = []
    with ConcurrencyGroupExecutor(
        parent_cg=mng_ctx.concurrency_group, name="send_message_to_agents", max_workers=32
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
                    _process_host_for_messaging,
                    host_ref=host_ref,
                    agent_refs=agent_refs,
                    provider=provider,
                    message_content=message_content,
                    compiled_include_filters=compiled_include_filters,
                    compiled_exclude_filters=compiled_exclude_filters,
                    all_agents=all_agents,
                    include_filters=include_filters,
                    error_behavior=error_behavior,
                    is_start_desired=is_start_desired,
                    result=result,
                    result_lock=result_lock,
                    parent_cg=mng_ctx.concurrency_group,
                    on_success=on_success,
                    on_error=on_error,
                )
            )

    # Re-raise any thread exceptions (e.g. abort-mode errors)
    for future in futures:
        future.result()

    return result


def _process_host_for_messaging(
    host_ref: HostReference,
    agent_refs: list[AgentReference],
    provider: BaseProviderInstance,
    message_content: str,
    compiled_include_filters: list[Any],
    compiled_exclude_filters: list[Any],
    all_agents: bool,
    include_filters: tuple[str, ...],
    error_behavior: ErrorBehavior,
    is_start_desired: bool,
    result: MessageResult,
    result_lock: Lock,
    parent_cg: ConcurrencyGroup,
    on_success: Callable[[str], None] | None,
    on_error: Callable[[str, str], None] | None,
) -> None:
    """Resolve a single host, filter its agents, and send messages concurrently.

    This function is run in a thread per host. Within it, per-agent sends are
    parallelized with a nested ConcurrencyGroupExecutor.
    """
    try:
        host_interface = provider.get_host(host_ref.host_id)

        # If host is offline, optionally start it or report an error
        if not isinstance(host_interface, OnlineHostInterface):
            if is_start_desired:
                host, _was_started = ensure_host_started(host_interface, is_start_desired=True, provider=provider)
            else:
                exception = HostOfflineError(f"Host '{host_ref.host_id}' is offline. Cannot send messages.")
                if error_behavior == ErrorBehavior.ABORT:
                    raise exception
                logger.warning("Host is offline: {}", host_ref.host_id)
                for agent_ref in agent_refs:
                    with result_lock:
                        result.failed_agents.append((str(agent_ref.agent_name), str(exception)))
                    if on_error:
                        on_error(str(agent_ref.agent_name), str(exception))
                return
        else:
            host = host_interface

        # Get all agents on this host and filter
        agents = host.get_agents()
        agents_to_send: list[AgentInterface] = []

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

            # Apply CEL filters if provided
            if compiled_include_filters or compiled_exclude_filters or not all_agents:
                agent_context = _agent_to_cel_context(agent, host_ref.provider_name)
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

            agents_to_send.append(agent)

        # Send messages to matching agents concurrently
        send_futures: list[Future[None]] = []
        with ConcurrencyGroupExecutor(
            parent_cg=parent_cg, name=f"send_msgs_{host_ref.host_id}", max_workers=32
        ) as send_executor:
            for agent in agents_to_send:
                send_futures.append(
                    send_executor.submit(
                        _send_message_to_agent,
                        agent=agent,
                        host=host,
                        message_content=message_content,
                        result=result,
                        result_lock=result_lock,
                        error_behavior=error_behavior,
                        is_start_desired=is_start_desired,
                        on_success=on_success,
                        on_error=on_error,
                    )
                )

        # Re-raise any send failures in ABORT mode
        for future in send_futures:
            future.result()

    except MngError as e:
        if error_behavior == ErrorBehavior.ABORT:
            raise
        logger.warning("Error accessing host {}: {}", host_ref.host_id, e)


def _send_message_to_agent(
    agent: AgentInterface,
    host: OnlineHostInterface,
    message_content: str,
    result: MessageResult,
    result_lock: Lock,
    error_behavior: ErrorBehavior,
    is_start_desired: bool,
    on_success: Callable[[str], None] | None,
    on_error: Callable[[str, str], None] | None,
) -> None:
    """Send a message to a single agent.

    Called from a worker thread. Known errors (BaseMngError) are recorded in
    `result`; in ABORT mode they are also re-raised so the ConcurrencyGroup
    propagates them.
    """
    agent_name = str(agent.name)

    # Check if agent has a tmux session (only STOPPED agents cannot receive messages)
    lifecycle_state = agent.get_lifecycle_state()
    if lifecycle_state == AgentLifecycleState.STOPPED:
        if is_start_desired:
            ensure_agent_started(agent, host, is_start_desired=True)
        else:
            error_msg = f"Agent has no tmux session (state: {lifecycle_state.value})"
            with result_lock:
                result.failed_agents.append((agent_name, error_msg))
            if on_error:
                on_error(agent_name, error_msg)
            if error_behavior == ErrorBehavior.ABORT:
                raise MngError(f"Cannot send message to {agent_name}: {error_msg}")
            return

    try:
        with log_span("Sending message to agent {}", agent_name):
            agent.send_message(message_content)
        with result_lock:
            result.successful_agents.append(agent_name)
        if on_success:
            on_success(agent_name)
    except BaseMngError as e:
        error_msg = str(e)
        with result_lock:
            result.failed_agents.append((agent_name, error_msg))
        if on_error:
            on_error(agent_name, error_msg)
        if error_behavior == ErrorBehavior.ABORT:
            raise MngError(error_msg) from e


def _agent_to_cel_context(agent: AgentInterface, provider_name: str) -> dict[str, Any]:
    """Convert an agent to a CEL-friendly dict for filtering."""
    return {
        "id": str(agent.id),
        "name": str(agent.name),
        "type": str(agent.agent_type),
        "state": agent.get_lifecycle_state().value,
        "host": {
            "id": str(agent.host_id),
            "provider": provider_name,
        },
    }
