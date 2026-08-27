from collections.abc import Callable
from collections.abc import Sequence
from concurrent.futures import Future
from threading import Lock

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_call
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import ensure_agent_started
from imbue.mngr.api.find import ensure_host_started
from imbue.mngr.api.find import group_agents_by_host
from imbue.mngr.api.find import revive_done_agent
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.errors import HostOfflineError
from imbue.mngr.errors import MessageDeliveredButBlockedError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import SendFailureKind
from imbue.mngr.errors import SendMessageError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import require_interactive_agent
from imbue.mngr.interfaces.agent import require_key_chord_agent
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.utils.thread_cleanup import mngr_executor


class AgentSendFailure(FrozenModel):
    """One agent that did not get the message: why, and what kind of thing went wrong.

    ``reason`` is the why ALONE -- "the agent is in shell mode with an unsubmitted command" --
    with no "failed to send to X" framing around it. Every consumer has ``agent_name`` right
    here and adds its own framing: the CLI logs "Failed to send message to {name}: {reason}",
    and a GUI puts it under a title of its own. Carrying the framing in the string meant each
    of them printed it twice.

    ``kind`` is the machine-readable half, for a client choosing what to offer: trying again
    helps a blocked input and cannot help an agent that is gone. Reading that out of prose
    written for a human, and varying per harness, is not something a client should have to do.
    """

    agent_name: str
    reason: str
    kind: SendFailureKind


class MessageResult(MutableModel):
    """Result of sending messages to agents."""

    successful_agents: list[str] = Field(
        default_factory=list, description="List of agent names that received messages"
    )
    failures: list[AgentSendFailure] = Field(
        default_factory=list, description="One record per agent that did not receive the message"
    )

    @property
    def failed_agents(self) -> list[tuple[str, str]]:
        """``(agent_name, reason)`` pairs -- the shape ``mngr message``'s exit code and output read."""
        return [(failure.agent_name, failure.reason) for failure in self.failures]

    blocked_agents: list[tuple[str, str]] = Field(
        default_factory=list,
        description="List of (agent_name, dialog_description) tuples for messages that were delivered but "
        "left the agent stuck on an unresolved blocking dialog",
    )


# One delivery of a payload to a single live agent (the payload is a message string
# for a text send, a tmux key token for a key chord). Raises an mngr error the fan-out
# records; a MessageDeliveredButBlockedError is treated as a delivered-but-blocked send.
DeliverToAgent = Callable[[AgentInterface, str], None]


def _deliver_text(agent: AgentInterface, payload: str) -> None:
    """Deliver ``payload`` as an interactive text message (the default fan-out action)."""
    require_interactive_agent(agent).send_message(payload)


def _deliver_key_chord(agent: AgentInterface, payload: str) -> None:
    """Deliver ``payload`` as a single tmux key token pressed into the agent's pane."""
    require_key_chord_agent(agent).press_key_chord(payload)


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
    return _deliver_to_agents(
        mngr_ctx=mngr_ctx,
        payload=message_content,
        deliver=_deliver_text,
        agents_to_message=agents_to_message,
        error_behavior=error_behavior,
        is_start_desired=is_start_desired,
        on_success=on_success,
        on_error=on_error,
    )


@log_call
def send_key_chord_to_agents(
    mngr_ctx: MngrContext,
    key: str,
    agents_to_message: Sequence[AgentMatch],
    error_behavior: ErrorBehavior = ErrorBehavior.CONTINUE,
    is_start_desired: bool = False,
    on_success: Callable[[str], None] | None = None,
    on_error: Callable[[str, str], None] | None = None,
) -> MessageResult:
    """Press ONE tmux key token (e.g. ``"M-q"``) into a pre-resolved set of agents' panes.

    The keystroke sibling of :func:`send_message_to_agents`: same host-grouped,
    concurrent fan-out and same per-agent ``message.lock`` serialization (via
    ``press_key_chord``), but it presses a raw key rather than pasting text + Enter.
    The lock is what keeps a chord from landing between a concurrent text send's paste
    and its Enter. ``key`` is a tmux ``send-keys`` token; the caller owns the choice of
    which key. Agent types with no tmux pane (headless, or an API-driven harness like
    pi/opencode) are refused per-agent via ``require_key_chord_agent``.
    """
    return _deliver_to_agents(
        mngr_ctx=mngr_ctx,
        payload=key,
        deliver=_deliver_key_chord,
        agents_to_message=agents_to_message,
        error_behavior=error_behavior,
        is_start_desired=is_start_desired,
        on_success=on_success,
        on_error=on_error,
    )


def _deliver_to_agents(
    mngr_ctx: MngrContext,
    payload: str,
    deliver: DeliverToAgent,
    agents_to_message: Sequence[AgentMatch],
    error_behavior: ErrorBehavior,
    is_start_desired: bool,
    on_success: Callable[[str], None] | None,
    on_error: Callable[[str, str], None] | None,
) -> MessageResult:
    """Fan ``deliver(payload)`` out to a pre-resolved set of agents, grouped by host.

    The shared engine behind ``send_message_to_agents`` (text) and
    ``send_key_chord_to_agents`` (key chord). Hosts are resolved and deliveries run
    concurrently so one slow host or one agent's failure does not block the others.
    """
    result = MessageResult()
    result_lock = Lock()

    matches_by_host = group_agents_by_host(agents_to_message)
    logger.trace("Delivering to agents across {} hosts", len(matches_by_host))

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
                    message_content=payload,
                    deliver=deliver,
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


def _record_agent_failure(
    result: MessageResult,
    result_lock: Lock,
    agent_name: str,
    error_msg: str,
    on_error: Callable[[str, str], None] | None,
    kind: SendFailureKind = SendFailureKind.UNKNOWN,
) -> None:
    """Record one agent as not having received the message.

    ``result.failures`` is what carries a failure into ``mngr message``'s exit code, and
    ``on_error`` is what carries it into the streamed ``--format jsonl`` output. ``error_msg``
    is the reason alone -- see :class:`AgentSendFailure` for why it carries no framing.
    """
    with result_lock:
        result.failures.append(AgentSendFailure(agent_name=agent_name, reason=error_msg, kind=kind))
    if on_error:
        on_error(agent_name, error_msg)


def _resolve_online_host(
    host_id: HostId, provider: BaseProviderInstance, is_start_desired: bool
) -> OnlineHostInterface:
    """Return the online host to message on, starting it when it is offline and that is desired."""
    host_interface = provider.get_host(host_id)
    if isinstance(host_interface, OnlineHostInterface):
        return host_interface
    if not is_start_desired:
        raise HostOfflineError(f"Host '{host_id}' is offline. Cannot send messages.")
    host, _was_started = ensure_host_started(host_interface, is_start_desired=True, provider=provider)
    return host


def _process_host_for_messaging(
    matches: Sequence[AgentMatch],
    provider: BaseProviderInstance,
    message_content: str,
    deliver: DeliverToAgent,
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
    parallelized with a nested ConcurrencyGroupExecutor. A host that cannot be reached
    fails every agent on it, so the result -- and the command's exit code -- report the
    message as undelivered rather than as nothing at all. ABORT additionally re-raises,
    so the failure leaves this call as an exception instead of only as a recorded result.
    """
    host_id = matches[0].host_id
    try:
        host = _resolve_online_host(host_id, provider, is_start_desired=is_start_desired)
        # Look up live agents on the host that correspond to our matches
        live_agents = host.get_agents()
    except MngrError as e:
        # No agent on this host was reached, so each of them is a delivery failure.
        # Recorded before the abort check, so an aborted run still reports which agents
        # missed the message -- the raise ends the command either way.
        for match in matches:
            _record_agent_failure(result, result_lock, str(match.agent_name), str(e), on_error)
        if error_behavior == ErrorBehavior.ABORT:
            raise
        logger.warning("Error accessing host {}: {}", host_id, e)
        return

    try:
        agents_to_send: list[AgentInterface] = []

        for match in matches:
            agent = next((a for a in live_agents if a.id == match.agent_id), None)
            if agent is None:
                exception = AgentNotFoundOnHostError(match.agent_id, host_id)
                _record_agent_failure(result, result_lock, str(match.agent_name), str(exception), on_error)
                if error_behavior == ErrorBehavior.ABORT:
                    raise exception
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
                        deliver=deliver,
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
        # Every agent reached here has recorded its own outcome, and re-recording would
        # append a second entry rather than replace the first. What is left is the
        # executor itself failing, which belongs to no single agent.
        logger.warning("Error sending messages on host {}: {}", host_id, e)


def _send_message_to_agent(
    agent: AgentInterface,
    host: OnlineHostInterface,
    message_content: str,
    deliver: DeliverToAgent,
    result: MessageResult,
    result_lock: Lock,
    error_behavior: ErrorBehavior,
    is_start_desired: bool,
    on_success: Callable[[str], None] | None,
    on_error: Callable[[str, str], None] | None,
) -> None:
    """Deliver ``message_content`` to a single agent via ``deliver``.

    Called from a worker thread. Known errors (MngrError) are recorded in
    `result`; in ABORT mode they are also re-raised so the ConcurrencyGroup
    propagates them.
    """
    agent_name = str(agent.name)

    # (Re)start the agent unless it is live enough to receive a message. STOPPED
    # has no tmux session at all; DONE has a lingering session whose agent process
    # already exited (a ctrl-c, a crash, or an OOM shed leaves tmux holding the
    # pane open on a bare shell). In both cases there is no agent to deliver to, so
    # a raw send would just type the message into a dead shell and silently lose
    # it. A DONE husk must be torn down before the relaunch actually happens
    # (revive_done_agent), whereas a STOPPED agent just needs a plain start.
    try:
        lifecycle_state = agent.get_lifecycle_state()
    except MngrError as e:
        error_msg = str(e)
        _record_agent_failure(result, result_lock, agent_name, error_msg, on_error)
        if error_behavior == ErrorBehavior.ABORT:
            raise MngrError(error_msg) from e
        return

    if lifecycle_state in (AgentLifecycleState.STOPPED, AgentLifecycleState.DONE):
        if is_start_desired:
            try:
                if lifecycle_state == AgentLifecycleState.DONE:
                    revive_done_agent(agent, host)
                else:
                    ensure_agent_started(agent, host, is_start_desired=True)
            except MngrError as e:
                error_msg = str(e)
                # The agent was stopped and would not start, so there is nothing to type into.
                # This is the path a chat send actually takes when an agent has died -- it asks
                # for a start rather than failing on the stopped state above -- so leaving it
                # unclassified is what would offer a retry that cannot work.
                _record_agent_failure(
                    result, result_lock, agent_name, error_msg, on_error, SendFailureKind.AGENT_UNREACHABLE
                )
                if error_behavior == ErrorBehavior.ABORT:
                    raise MngrError(error_msg) from e
                return
        else:
            error_msg = f"Agent is not running (state: {lifecycle_state.value})"
            _record_agent_failure(
                result, result_lock, agent_name, error_msg, on_error, SendFailureKind.AGENT_UNREACHABLE
            )
            if error_behavior == ErrorBehavior.ABORT:
                raise MngrError(f"Cannot send message to {agent_name}: {error_msg}")
            return

    try:
        with log_span("Delivering to agent {}", agent_name):
            deliver(agent, message_content)
        with result_lock:
            result.successful_agents.append(agent_name)
        if on_success:
            on_success(agent_name)
    except MessageDeliveredButBlockedError as e:
        # The message WAS delivered/accepted; only a post-delivery blocking dialog could not
        # be cleared. Record it separately from a real send failure so the CLI can surface a
        # distinct exit code. Preserve the original exception type on abort re-raise.
        error_msg = str(e)
        with result_lock:
            result.blocked_agents.append((agent_name, error_msg))
        if on_error:
            on_error(agent_name, error_msg)
        if error_behavior == ErrorBehavior.ABORT:
            raise
    except MngrError as e:
        error_msg = str(e)
        # A harness that classified its own failure says so on the exception, and states the
        # reason without the "failed to send to X" framing str(e) adds for a standalone raise.
        # Anything else is unclassified prose, which the client treats exactly as it does today.
        reason = e.reason if isinstance(e, SendMessageError) else error_msg
        kind = e.kind if isinstance(e, SendMessageError) else SendFailureKind.UNKNOWN
        _record_agent_failure(result, result_lock, agent_name, reason, on_error, kind)
        if error_behavior == ErrorBehavior.ABORT:
            raise MngrError(error_msg) from e


def send_message_with_resend_guidance(agent: AgentInterface, message: str, situation: str) -> None:
    """Send a message, framing a delivery failure so the caller knows the agent itself is fine.

    Used by the create-with-initial-message and resume paths, where a failed
    send would otherwise read as the whole command failing: the agent is up
    and healthy, only the message was not delivered.
    """
    try:
        require_interactive_agent(agent).send_message(message)
    except MessageDeliveredButBlockedError:
        # The message WAS delivered; only a post-delivery dialog is blocking. Do not reframe
        # this as "NOT delivered" -- propagate it unchanged so its distinct meaning survives.
        raise
    except SendMessageError as e:
        raise SendMessageError(
            e.agent_name,
            f"{e.reason}\n\nThe agent is up ({situation}), but the message was NOT delivered. "
            f"Resend it with: mngr message {agent.name}",
        ) from e
