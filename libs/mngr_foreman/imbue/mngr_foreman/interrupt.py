"""Send an interrupt (Escape key) to a running agent's tmux pane.

Claude's TUI treats Escape as "stop generating" / cancel the current prompt.
mngr has no interrupt API, so foreman does what a human at the terminal would:
resolve the agent to its live host + tmux session and run
``tmux send-keys -t '=<session>' Escape`` there via
``OnlineHostInterface.execute_stateful_command``. The ``=`` prefix forces
exact session-name matching so the keystroke can never land on a different
agent's session (see ``TmuxSessionTarget``).
"""

from __future__ import annotations

from imbue.mngr.api.address_parsers import parse_agent_address
from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.find import resolve_to_started_host_and_agent
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.tmux import TmuxWindowTarget

# Bound the send-keys command so an unresponsive host can't wedge the interrupt.
_HOST_COMMAND_TIMEOUT_SECONDS = 10.0


class InterruptError(Exception):
    """Raised when the interrupt keystroke could not be delivered."""


def send_interrupt_to_agent(mngr_ctx: MngrContext, agent_name: str) -> None:
    """Send Escape to ``agent_name``'s tmux pane on its host.

    Does not auto-start the host/agent -- an interrupt only makes sense for a
    running agent. Raises :class:`InterruptError` if the agent cannot be
    resolved, its host is not online, or the ``tmux send-keys`` command fails.
    """
    address = parse_agent_address(agent_name)
    try:
        host_ref, agent_ref = find_one_agent(address, mngr_ctx)
        agent, host = resolve_to_started_host_and_agent(
            host_ref=host_ref,
            agent_ref=agent_ref,
            allow_auto_start=False,
            mngr_ctx=mngr_ctx,
        )
    except Exception as e:  # noqa: BLE001 - surface any resolution failure to the caller
        raise InterruptError(f"Could not resolve running agent {agent_name!r}: {e}") from e

    # send-keys needs a window/pane target, not a bare session -- target the
    # agent's primary window exactly as mngr does when it sends messages
    # (base_agent.tmux_target), so the keystroke lands on the claude pane.
    target = TmuxWindowTarget(
        session_name=agent.session_name,
        window=mngr_ctx.config.tmux.primary_window_name,
    )
    command = f"tmux send-keys -t {target.as_shell_arg()} Escape"
    try:
        result = host.execute_stateful_command(command, timeout_seconds=_HOST_COMMAND_TIMEOUT_SECONDS)
    except Exception as e:  # noqa: BLE001 - host/exec errors become a clean interrupt error
        raise InterruptError(f"Failed to send interrupt to {agent_name!r}: {e}") from e

    if not result.success:
        raise InterruptError(result.stderr or result.stdout or "tmux send-keys Escape failed")
