import shlex
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OnlineHostInterface

# Default timeout for tmux capture-pane operations
_DEFAULT_CAPTURE_PANE_TIMEOUT_SECONDS: Final[float] = 5.0

# Messages at or above this length use load-buffer/paste-buffer instead of send-keys
# to avoid tmux "command too long" errors. Used by both base_agent.py and host.py.
LONG_MESSAGE_THRESHOLD: Final[int] = 1024


class TmuxSessionTarget(FrozenModel):
    """Structured tmux ``-t`` target for commands whose target resolves as a session.

    Use for commands that accept a target-session (e.g. ``has-session``,
    ``kill-session``, ``rename-session``, ``attach``, ``list-windows`` with ``-t``
    pointing at a session).

    The leading ``=`` produced by :meth:`as_shell_arg` forces exact session-name
    matching; without it, tmux silently falls back to *prefix* matching, so a
    query for ``mngr-foo`` would match a live session called ``mngr-foo-bar`` when
    the exact session no longer exists. That misrouting can deliver keystrokes to
    the wrong agent, kill the wrong session, or capture the wrong pane.
    """

    session_name: str = Field(min_length=1)

    def as_target_arg(self) -> str:
        """Return the raw, exact-match ``-t`` value (``=<session>``), unquoted.

        Use in argv contexts that bypass the shell (a command token list handed
        directly to exec), where shell quoting would wrongly become part of the
        argument. For shell-command strings use :meth:`as_shell_arg` instead.
        """
        return f"={self.session_name}"

    def as_shell_arg(self) -> str:
        """Return the shell-quoted, exact-match ``-t`` argument string.

        Drop directly into a shell f-string after ``-t``::

            cmd = f"tmux has-session -t {target.as_shell_arg()}"
        """
        return shlex.quote(self.as_target_arg())


class TmuxWindowTarget(FrozenModel):
    """Structured tmux ``-t`` target for commands whose target resolves as a window or pane.

    Use for commands that accept a target-window or target-pane (e.g.
    ``list-panes`` without ``-s``, ``send-keys``, ``capture-pane``,
    ``paste-buffer``, ``set-option``, ``select-window``, ``new-window``,
    ``resize-window``, ``split-window``).

    Same exact-session-matching motivation as :class:`TmuxSessionTarget`, with one
    extra twist: the explicit ``:window`` component is *required* for target-window/
    -pane commands. Without it, tmux parses ``=name`` as a literal window/pane name
    (which never exists) and the command fails with ``can't find pane: =name``.

    Note: ``list-panes -s`` is NOT covered by this helper. Despite its ``-t`` being
    documented as a target-session form, tmux's ``cmd-find.c`` ignores the ``=``
    prefix on it. For "all panes in this session", iterate windows via
    :class:`TmuxSessionTarget` + ``list-windows``, then call ``list-panes`` per
    window with a :class:`TmuxWindowTarget`.
    """

    session_name: str = Field(min_length=1)
    window: int | str = Field(default=0)

    def as_shell_arg(self) -> str:
        """Return the shell-quoted, exact-match ``-t`` argument string."""
        return shlex.quote(f"={self.session_name}:{self.window}")


def build_tmux_capture_pane_command(target: TmuxWindowTarget, include_scrollback: bool = False) -> str:
    """Build the tmux command string to capture pane content for a target.

    When include_scrollback is True, uses ``-S -`` to capture from the start of the
    scrollback buffer instead of just the visible pane.
    """
    scrollback_flag = " -S -" if include_scrollback else ""
    return f"tmux capture-pane -t {target.as_shell_arg()}{scrollback_flag} -p"


def capture_tmux_pane_content(
    host: OnlineHostInterface,
    target: TmuxWindowTarget,
    timeout_seconds: float = _DEFAULT_CAPTURE_PANE_TIMEOUT_SECONDS,
    include_scrollback: bool = False,
) -> str | None:
    """Capture the current tmux pane content via a host, returning None on failure.

    This is the canonical implementation for capturing tmux pane content through
    a host's command execution layer (which works both locally and over SSH).

    When include_scrollback is True, captures the full scrollback buffer.
    """
    result: CommandResult = host.execute_idempotent_command(
        build_tmux_capture_pane_command(target, include_scrollback=include_scrollback),
        timeout_seconds=timeout_seconds,
    )
    if result.success:
        return result.stdout.rstrip()
    return None
