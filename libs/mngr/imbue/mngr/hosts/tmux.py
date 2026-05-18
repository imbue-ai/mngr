from typing import Final

from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OnlineHostInterface

# Default timeout for tmux capture-pane operations
_DEFAULT_CAPTURE_PANE_TIMEOUT_SECONDS: Final[float] = 5.0

# Messages at or above this length use load-buffer/paste-buffer instead of send-keys
# to avoid tmux "command too long" errors. Used by both base_agent.py and host.py.
LONG_MESSAGE_THRESHOLD: Final[int] = 1024

# Bound the time a single tmux client call (send-keys, load-buffer, paste-buffer,
# delete-buffer) may run for. These should complete in milliseconds; a multi-second
# wait means the tmux client is wedged talking to its server, which has happened
# in practice. Because send_message holds an exclusive flock on the per-agent
# message lock for the entire send, an unbounded wait here wedges every subsequent
# send to the same agent until something kills the stuck client.
TMUX_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0


def build_tmux_capture_pane_command(session_name: str, include_scrollback: bool = False) -> str:
    """Build the tmux command string to capture pane content for a session.

    When include_scrollback is True, uses ``-S -`` to capture from the start of the
    scrollback buffer instead of just the visible pane.
    """
    scrollback_flag = " -S -" if include_scrollback else ""
    return f"tmux capture-pane -t '{session_name}'{scrollback_flag} -p"


def capture_tmux_pane_content(
    host: OnlineHostInterface,
    session_name: str,
    timeout_seconds: float = _DEFAULT_CAPTURE_PANE_TIMEOUT_SECONDS,
    include_scrollback: bool = False,
) -> str | None:
    """Capture the current tmux pane content via a host, returning None on failure.

    This is the canonical implementation for capturing tmux pane content through
    a host's command execution layer (which works both locally and over SSH).

    When include_scrollback is True, captures the full scrollback buffer.
    """
    result: CommandResult = host.execute_idempotent_command(
        build_tmux_capture_pane_command(session_name, include_scrollback=include_scrollback),
        timeout_seconds=timeout_seconds,
    )
    if result.success:
        return result.stdout.rstrip()
    return None
