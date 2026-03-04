from typing import Final

from imbue.mng.interfaces.data_types import CommandResult
from imbue.mng.interfaces.host import OnlineHostInterface

# Default timeout for tmux capture-pane operations
_DEFAULT_CAPTURE_PANE_TIMEOUT_SECONDS: Final[float] = 5.0


def build_tmux_capture_pane_command(session_name: str) -> str:
    """Build the tmux command string to capture pane content for a session."""
    return f"tmux capture-pane -t '{session_name}' -p"


def capture_tmux_pane_content(
    host: OnlineHostInterface,
    session_name: str,
    timeout_seconds: float = _DEFAULT_CAPTURE_PANE_TIMEOUT_SECONDS,
) -> str | None:
    """Capture the current tmux pane content via a host, returning None on failure.

    This is the canonical implementation for capturing tmux pane content through
    a host's command execution layer (which works both locally and over SSH).
    """
    result: CommandResult = host.execute_command(
        build_tmux_capture_pane_command(session_name),
        timeout_seconds=timeout_seconds,
    )
    if result.success:
        return result.stdout.rstrip()
    return None
