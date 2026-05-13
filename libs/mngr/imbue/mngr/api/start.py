from collections.abc import Callable

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.utils.polling import poll_until


def send_resume_message_if_configured(
    agent: AgentInterface,
    on_status: Callable[[str], None] | None = None,
) -> None:
    """Send the agent's configured resume_message after it reaches WAITING.

    Waits for the agent to signal readiness via the WAITING lifecycle state
    before sending. If readiness is not reached within the agent's
    ready_timeout_seconds, the message is sent anyway. No-op when the agent
    has no resume_message configured.
    """
    resume_message = agent.get_resume_message()
    if resume_message is None:
        return

    if on_status is not None:
        on_status(f"Sending resume message to {agent.name}...")

    timeout = agent.get_ready_timeout_seconds()
    with log_span("Waiting for agent to become ready before sending resume message"):
        is_ready = poll_until(
            lambda: agent.get_lifecycle_state() == AgentLifecycleState.WAITING,
            timeout=timeout,
            poll_interval=0.2,
        )
    if is_ready:
        logger.debug("Signaled agent readiness via WAITING state")
    else:
        logger.debug(
            "Failed to reach WAITING state within {}s, proceeding anyway",
            timeout,
        )
    agent.send_message(resume_message)
    logger.debug("Sent resume message to agent {}", agent.name)
