import shlex
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OnlineHostInterface

# Default timeout for tmux capture-pane operations
_DEFAULT_CAPTURE_PANE_TIMEOUT_SECONDS: Final[float] = 5.0

# Messages at or above this length use load-buffer/paste-buffer instead of send-keys
# to avoid tmux "command too long" errors. Used by both base_agent.py and host.py.
LONG_MESSAGE_THRESHOLD: Final[int] = 1024


@pure
def tmux_session_target(session_name: str) -> str:
    """Build a tmux ``-t`` target string for a command whose target resolves as a session.

    Use for commands that accept a target-session (e.g. ``has-session``,
    ``kill-session``, ``rename-session``, ``attach``, ``list-windows`` with ``-t``
    pointing at a session). The leading ``=`` forces exact session-name matching;
    without it, tmux silently falls back to *prefix* matching, so e.g. a query
    for ``mngr-foo`` will match a live session called ``mngr-foo-bar`` when the
    exact session no longer exists. That misrouting once caused stopped agents
    to report as ``WAITING`` because the lifecycle check landed in another
    agent's session and read its still-running process.

    Always construct tmux ``-t`` targets via this helper (or
    :func:`tmux_window_target`) rather than inlining ``f"={name}"`` -- the
    ``test_no_bare_tmux_targets`` ratchet enforces this.
    """
    return f"={session_name}"


@pure
def tmux_window_target(session_name: str, window: int | str = 0) -> str:
    """Build a tmux ``-t`` target string for a command whose target resolves as a window or pane.

    Use for commands that accept a target-window or target-pane (e.g.
    ``list-panes`` without ``-s``, ``send-keys``, ``capture-pane``,
    ``paste-buffer``, ``set-option``, ``select-window``, ``new-window``,
    ``resize-window``, ``split-window``).

    Returns ``"={session_name}:{window}"``. Two non-obvious points:

    * The leading ``=`` forces exact session-name matching (preventing tmux's
      default prefix matching from misrouting the command to a sibling session
      whose name starts with ``session_name``).
    * The explicit ``:window`` component is *required* for target-window/-pane
      commands. Without it, tmux parses ``=name`` as an exact match on a
      *window or pane* literally called ``=name`` (which never exists) and the
      command fails with ``can't find pane: =name``. With ``:window``, tmux
      parses ``=name`` as the session component (exact match) and ``window`` as
      the window component, which is what we want.

    For ``list-panes -s`` (which lists across all windows of a session) the
    ``-s`` makes ``-t`` notionally a target-session, but tmux's ``cmd-find.c``
    still routes it through window resolution, so neither ``=name`` nor
    ``=name:0`` works as exact session match -- guard with
    ``tmux has-session -t {tmux_session_target(name)}`` and then pass the bare
    name to ``list-panes -s``.
    """
    return f"={session_name}:{window}"


@pure
def build_tmux_capture_pane_command(target: str, include_scrollback: bool = False) -> str:
    """Build the tmux command string to capture pane content for a target.

    ``target`` must be a fully-formed tmux ``-t`` argument produced via
    :func:`tmux_window_target` (or, in rare cases, a target-session form for
    ``capture-pane`` if you want the active pane of the active window).

    When include_scrollback is True, uses ``-S -`` to capture from the start of the
    scrollback buffer instead of just the visible pane.
    """
    scrollback_flag = " -S -" if include_scrollback else ""
    return f"tmux capture-pane -t {shlex.quote(target)}{scrollback_flag} -p"


def capture_tmux_pane_content(
    host: OnlineHostInterface,
    target: str,
    timeout_seconds: float = _DEFAULT_CAPTURE_PANE_TIMEOUT_SECONDS,
    include_scrollback: bool = False,
) -> str | None:
    """Capture the current tmux pane content via a host, returning None on failure.

    This is the canonical implementation for capturing tmux pane content through
    a host's command execution layer (which works both locally and over SSH).

    ``target`` must be a fully-formed tmux ``-t`` argument produced via
    :func:`tmux_window_target`.

    When include_scrollback is True, captures the full scrollback buffer.
    """
    result: CommandResult = host.execute_idempotent_command(
        build_tmux_capture_pane_command(target, include_scrollback=include_scrollback),
        timeout_seconds=timeout_seconds,
    )
    if result.success:
        return result.stdout.rstrip()
    return None
