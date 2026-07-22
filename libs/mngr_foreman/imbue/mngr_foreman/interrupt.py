"""Send an interrupt (Escape key) to a running agent's tmux pane.

Claude's TUI treats Escape as "stop generating" / cancel the current prompt.
mngr has no interrupt API, so foreman does what a human at the terminal would:
resolve the agent to its live host + tmux session and run
``tmux send-keys -t '=<session>' Escape`` there via
``OnlineHostInterface.execute_stateful_command``. The ``=`` prefix forces
exact session-name matching so the keystroke can never land on a different
agent's session (see ``TmuxSessionTarget``).

The send-keys runs through ``pool.run_on_host`` so it (1) reuses the always-warm
connection instead of paying mngr's ~3s discovery on every Escape press, and (2)
runs UNDER the per-host lock -- driving the shared SSH connection concurrently with a
transcript read / pane probe corrupts the protocol and drops it for every agent on
that host.
"""

from __future__ import annotations

from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_foreman.connection_pool import ConnectionPool

# Bound the send-keys command so an unresponsive host can't wedge the interrupt.
_HOST_COMMAND_TIMEOUT_SECONDS = 10.0


class InterruptError(Exception):
    """Raised when the interrupt keystroke could not be delivered."""


def send_interrupt_to_agent(pool: ConnectionPool, agent_name: str) -> None:
    """Send Escape to ``agent_name``'s tmux pane on its host, via the warm pool.

    Does not auto-start the host/agent -- an interrupt only makes sense for a
    running agent. Raises :class:`InterruptError` if the agent cannot be
    resolved, its host is busy/offline, or the ``tmux send-keys`` command fails.
    """

    def _interrupt(agent: AgentInterface, host: OnlineHostInterface) -> None:
        # send-keys needs a window/pane target, not a bare session -- target the
        # agent's primary window exactly as mngr does when it sends messages, so the
        # keystroke lands on the claude pane.
        target = TmuxWindowTarget(
            session_name=agent.session_name,
            window=pool.mngr_ctx.config.tmux.primary_window_name,
        )
        command = f"tmux send-keys -t {target.as_shell_arg()} Escape"
        result = host.execute_stateful_command(command, timeout_seconds=_HOST_COMMAND_TIMEOUT_SECONDS)
        if not result.success:
            raise InterruptError(result.stderr or result.stdout or "tmux send-keys Escape failed")

    try:
        pool.run_on_host(agent_name, _interrupt)
    except InterruptError:
        raise
    except Exception as e:  # noqa: BLE001 - resolution / busy-host / exec errors -> clean interrupt error
        raise InterruptError(f"Failed to send interrupt to {agent_name!r}: {e}") from e
