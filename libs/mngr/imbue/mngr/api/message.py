from collections.abc import Callable
from collections.abc import Sequence
from concurrent.futures import Future
from threading import Lock

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.logging import log_call
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import ensure_agent_started
from imbue.mngr.api.find import ensure_host_started
from imbue.mngr.api.find import group_agents_by_host
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.errors import HostOfflineError
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import require_interactive_agent
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.utils.thread_cleanup import mngr_executor


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
    mngr_ctx: MngrContext,
    message_content: str,
    agents_to_message: Sequence[AgentMatch],
    error_behavior: ErrorBehavior = ErrorBehavior.CONTINUE,
    is_start_desired: bool = False,
    on_success: Callable[[str], None] | None = None,
    on_error: Callable[[str, str], None] | None = None,
) -> MessageResult:
    """Send a message to a pre-resolved set of agents, grouped by host.

    Hosts are resolved and messages are sent concurrently so that one slow host
    or one agent's failure does not block messages to other agents. Callers
    typically obtain ``agents_to_message`` from ``find_all_agents``.
    """
    result = MessageResult()
    result_lock = Lock()

    matches_by_host = group_agents_by_host(agents_to_message)
    logger.trace("Messaging agents across {} hosts", len(matches_by_host))

    futures: list[Future[None]] = []
    with mngr_executor(
        parent_cg=mngr_ctx.concurrency_group, name="send_message_to_agents", max_workers=32
    ) as executor:
        for matches_on_host in matches_by_host.values():
            provider = get_provider_instance(matches_on_host[0].provider_name, mngr_ctx)
            futures.append(
                executor.submit(
                    _process_host_for_messaging,
                    matches=matches_on_host,
                    provider=provider,
                    message_content=message_content,
                    error_behavior=error_behavior,
                    is_start_desired=is_start_desired,
                    result=result,
                    result_lock=result_lock,
                    parent_cg=mngr_ctx.concurrency_group,
                    on_success=on_success,
                    on_error=on_error,
                )
            )

    # Re-raise any thread exceptions (e.g. abort-mode errors)
    for future in futures:
        future.result()

    return result


def _process_host_for_messaging(
    matches: Sequence[AgentMatch],
    provider: BaseProviderInstance,
    message_content: str,
    error_behavior: ErrorBehavior,
    is_start_desired: bool,
    result: MessageResult,
    result_lock: Lock,
    parent_cg: ConcurrencyGroup,
    on_success: Callable[[str], None] | None,
    on_error: Callable[[str, str], None] | None,
) -> None:
    """Resolve a single host, look up its agents, and send messages concurrently.

    This function is run in a thread per host. Within it, per-agent sends are
    parallelized with a nested ConcurrencyGroupExecutor.
    """
    host_id = matches[0].host_id
    try:
        host_interface = provider.get_host(host_id)

        # If host is offline, optionally start it or report an error
        if not isinstance(host_interface, OnlineHostInterface):
            if is_start_desired:
                host, _was_started = ensure_host_started(host_interface, is_start_desired=True, provider=provider)
            else:
                exception = HostOfflineError(f"Host '{host_id}' is offline. Cannot send messages.")
                if error_behavior == ErrorBehavior.ABORT:
                    raise exception
                logger.warning("Host is offline: {}", host_id)
                for match in matches:
                    with result_lock:
                        result.failed_agents.append((str(match.agent_name), str(exception)))
                    if on_error:
                        on_error(str(match.agent_name), str(exception))
                return
        else:
            host = host_interface

        # Look up live agents on the host that correspond to our matches
        live_agents = host.get_agents()
        agents_to_send: list[AgentInterface] = []

        for match in matches:
            agent = next((a for a in live_agents if a.id == match.agent_id), None)
            if agent is None:
                exception = AgentNotFoundOnHostError(match.agent_id, host_id)
                if error_behavior == ErrorBehavior.ABORT:
                    raise exception
                error_msg = str(exception)
                with result_lock:
                    result.failed_agents.append((str(match.agent_name), error_msg))
                if on_error:
                    on_error(str(match.agent_name), error_msg)
                continue
            agents_to_send.append(agent)

        # Send messages to matching agents concurrently
        send_futures: list[Future[None]] = []
        with mngr_executor(parent_cg=parent_cg, name=f"send_msgs_{host_id}", max_workers=32) as send_executor:
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

    except MngrError as e:
        if error_behavior == ErrorBehavior.ABORT:
            raise
        logger.warning("Error accessing host {}: {}", host_id, e)


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

    Called from a worker thread. Known errors (MngrError) are recorded in
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
                raise MngrError(f"Cannot send message to {agent_name}: {error_msg}")
            return

    try:
        with log_span("Sending message to agent {}", agent_name):
            require_interactive_agent(agent).send_message(message_content)
        with result_lock:
            result.successful_agents.append(agent_name)
        if on_success:
            on_success(agent_name)
    except MngrError as e:
        error_msg = str(e)
        with result_lock:
            result.failed_agents.append((agent_name, error_msg))
        if on_error:
            on_error(agent_name, error_msg)
        if error_behavior == ErrorBehavior.ABORT:
            raise MngrError(error_msg) from e
