"""Send a message to one agent by name, matching ``mngr message`` semantics."""

from __future__ import annotations

from imbue.mngr.api.address_parsers import parse_agent_address
from imbue.mngr.api.find import find_all_agents
from imbue.mngr.api.message import send_message_to_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNotFoundError
from imbue.mngr.primitives import ErrorBehavior


class MessageSendError(Exception):
    """Raised when a message could not be delivered to the target agent."""


def send_message_to_agent(mngr_ctx: MngrContext, agent_name: str, message: str) -> None:
    """Resolve ``agent_name`` and deliver ``message`` to it.

    Auto-starts a stopped agent (``is_start_desired=True``). A blocking TUI
    dialog on the agent (e.g. an unanswered permission or ``/login`` prompt)
    makes the send fail; that failure string is surfaced verbatim so the UI can
    point the user at the terminal page (phase 2). Raises
    :class:`MessageSendError` on any failure.
    """
    address = parse_agent_address(agent_name)
    try:
        matches = find_all_agents(
            addresses=[address],
            filter_all=False,
            target_state=None,
            mngr_ctx=mngr_ctx,
        )
    except AgentNotFoundError as e:
        raise MessageSendError(f"No agent found matching {agent_name!r}: {e}") from e

    if not matches:
        raise MessageSendError(f"No agent found matching {agent_name!r}")

    result = send_message_to_agents(
        mngr_ctx=mngr_ctx,
        message_content=message,
        agents_to_message=matches,
        error_behavior=ErrorBehavior.CONTINUE,
        is_start_desired=True,
    )

    if result.failed_agents:
        # One target: surface its error string directly (blocking TUI dialogs
        # land here -- the UI hints at the terminal page in that case).
        _name, error = result.failed_agents[0]
        raise MessageSendError(error)
