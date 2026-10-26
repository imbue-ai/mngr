import re
import shlex
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OnlineHostInterface

# Default timeout for tmux capture-pane operations
_DEFAULT_CAPTURE_PANE_TIMEOUT_SECONDS: Final[float] = 5.0

# Messages at or above this length use load-buffer/paste-buffer instead of send-keys
# to avoid tmux "command too long" errors. Used by base_agent.py, for messages sent to an
# already-running agent. The launch path does not use this: it faces a different limit
# (a pty's canonical-mode input buffer, not tmux's argv limit) and sources a script instead.
LONG_MESSAGE_THRESHOLD: Final[int] = 1024


@pure
def quote_tmux_text_argument(text: str) -> str:
    r"""Shell-quote ``text`` as one tmux argument that tmux delivers exactly as written.

    tmux splits its own argv into commands at any argument ending in ``;`` and drops that ``;``,
    so ``send-keys -l -- 'hello;'`` types ``hello``. An argument ending in ``\;`` is kept, with
    its final ``\;`` read as ``;``. A backslash before a trailing ``;`` therefore round-trips
    every text, including one that itself ends in ``\;`` (``find ... -exec ... \;``).
    """
    escaped = text[:-1] + "\\;" if text.endswith(";") else text
    return shlex.quote(escaped)


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


# The tmux session user-option holding the agent pane's ID. `@`-prefixed names are tmux's user
# namespace: tmux stores the string and never interprets it, and the value dies with the session,
# so it describes exactly as long as the thing it describes exists.
AGENT_PANE_ID_OPTION: Final[str] = "@mngr_agent_pane"

# The tmux session user-option holding the agent's id. A session is named after its agent, but a
# rename moves the session before it rewrites the agent's data.json, so whoever loaded the name
# just before can find no session by it; the id never changes, and the option travels with the
# session through `rename-session`.
AGENT_ID_OPTION: Final[str] = "@mngr_agent_id"

# What a tmux client prints when no server listens on its socket, so there are no sessions: "no
# server running on <socket>" for a stale socket, "error connecting to <socket> (No such file or
# directory)" for none.
TMUX_NO_SERVER_STDERR_SUBSTRINGS: Final[tuple[str, ...]] = ("no server running on ", "(No such file or directory)")

# Shell expressions for the name and id of the tmux session a command's own pane belongs to. A
# launch command is stored once at create and replayed on every start, so one that baked in the
# session's name would name a gone session after the agent is renamed; these read it when they run.
PANE_SESSION_NAME_SHELL_EXPRESSION: Final[str] = '"$(tmux display-message -p -t "$TMUX_PANE" \'#{session_name}\')"'
PANE_SESSION_ID_SHELL_EXPRESSION: Final[str] = '"$(tmux display-message -p -t "$TMUX_PANE" \'#{session_id}\')"'

# A session name as an older mngr's launch command passed it: `shlex.quote`d, so bare unless it needed quoting.
_LITERAL_SESSION_NAME_ARGUMENT: Final[str] = r"(?:'[^']*'|[^\s'\"$()]+)"


# CLEANUP: drop this and the agent types' upgrade_stored_command rewrites that use it once every agent
# whose launch command an older mngr stored has been started on an mngr that upgrades it (or destroyed).
@pure
def upgrade_literal_session_name_argument(command: str, invocation_prefix: str) -> str:
    """Return ``command`` with the session name following ``invocation_prefix`` rewritten to read the pane's session at run time."""
    pattern = re.compile(re.escape(invocation_prefix) + " " + _LITERAL_SESSION_NAME_ARGUMENT + r"(?=[\s)])")
    return pattern.sub(lambda _match: f"{invocation_prefix} {PANE_SESSION_NAME_SHELL_EXPRESSION}", command)


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


def build_tmux_capture_pane_command(
    target: TmuxWindowTarget | str, include_scrollback: bool = False, include_styles: bool = False
) -> str:
    """Build the tmux command string to capture pane content for a target.

    ``target`` may be an already-rendered ``-t`` argument (the agent's pane ID, resolved by the
    send path) as well as a window target, so a capture can be pointed at the same pane the sends
    go to rather than at whichever pane is active.

    When include_scrollback is True, uses ``-S -`` to capture from the start of the
    scrollback buffer instead of just the visible pane.

    When include_styles is True, uses ``-e`` to keep each cell's SGR attributes (colour, dim,
    reverse video) as escape sequences in the output.
    """
    target_arg = target if isinstance(target, str) else target.as_shell_arg()
    scrollback_flag = " -S -" if include_scrollback else ""
    styles_flag = " -e" if include_styles else ""
    return f"tmux capture-pane -t {target_arg}{scrollback_flag}{styles_flag} -p"


def capture_tmux_pane_content(
    host: OnlineHostInterface,
    target: TmuxWindowTarget | str,
    timeout_seconds: float = _DEFAULT_CAPTURE_PANE_TIMEOUT_SECONDS,
    include_scrollback: bool = False,
    include_styles: bool = False,
) -> str | None:
    """Capture the current tmux pane content via a host, returning None on failure.

    This is the canonical implementation for capturing tmux pane content through
    a host's command execution layer (which works both locally and over SSH).

    When include_scrollback is True, captures the full scrollback buffer. When include_styles is
    True, the content keeps each cell's SGR escape sequences.
    """
    result: CommandResult = host.execute_idempotent_command(
        build_tmux_capture_pane_command(target, include_scrollback=include_scrollback, include_styles=include_styles),
        timeout_seconds=timeout_seconds,
    )
    if result.success:
        return result.stdout.rstrip()
    return None
