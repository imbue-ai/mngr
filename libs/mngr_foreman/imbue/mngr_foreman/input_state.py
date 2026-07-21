"""Detect whether an agent's claude TUI is blocking on a dialog, plus mngr's
busy/idle signal -- the two inputs to the chat page's composer + working dot.

The composer sends a message by pasting into the agent's tmux input box. While
claude shows a blocking dialog (trust, permission, plan approval, AskUserQuestion,
/login, the model picker, ...) that paste can't be delivered, so the chat page
greys the composer and points at the terminal.

**One catch-all pane detector.** Every Claude Code dialog renders the same shape:
a numbered option list with the highlighted choice under a ``❯`` selection cursor,
e.g. ``❯ 1. Yes``. So rather than enumerate each dialog (trust/theme/cost/... each
with its own marker string), we key on that single shape -- the cursor on a
numbered option (``❯ <n>.``) *plus* at least one sibling numbered option
(``  <m>.``) in the bottom of the pane. The ready input row is ``❯`` (empty) or
``❯ <typed text>`` -- never ``❯ <digit>.`` -- and a slash command like ``❯ /login``
has no digit, so neither trips it. Requiring a sibling option guards against a
stray ``❯ 1.`` echoed in transcript content: a real menu always has >=2 options,
and the cursor line is anchored at line start so mid-line text can't match.

**Free pane-less signal.** mngr's own field generator publishes
``plugin.claude.waiting_reason == PERMISSIONS`` when claude is blocked on a
tool-approval dialog (``_waiting_reason`` in ``mngr_claude/plugin.py``). We read it
straight off the ``AgentDetails`` we already hold (``is_permissions_blocked``) and
OR it with the pane rule, so a permission prompt greys the composer even without a
tmux capture.

Detection is a single ``tmux capture-pane`` over the agent's host per poll; the
page polls it at a steady rate while the agent is running (over the warm pool, so
each probe is cheap).
"""

from __future__ import annotations

import re

from loguru import logger

from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_foreman.connection_pool import ConnectionPool


def is_busy_state(state: str | None) -> bool:
    """True when mngr reports claude actively working (its turn in progress).

    mngr promotes an agent to ``RUNNING`` only while the ``active`` marker file
    exists and the claude process is alive -- the marker is created by claude's
    UserPromptSubmit hook when a turn starts and removed by the Stop / idle-prompt
    hooks when it ends (see ``mngr_claude/claude_config.py``). Every other state is
    *not generating*: ``WAITING`` is idle at the prompt (END_OF_TURN) or blocked on
    a dialog (PERMISSIONS), and STOPPED / DONE / REPLACED / UNKNOWN are all
    non-running. So a single ``== RUNNING`` test is the authoritative busy/idle
    signal that drives the chat page's working dot off (the transcript heuristic
    turns it on instantly; this turns it off reliably).

    ``RUNNING_UNKNOWN_AGENT_TYPE`` cannot arise for a claude agent (its type is
    known), so it is deliberately *not* treated as busy here.
    """
    return (state or "").upper() == "RUNNING"


def waiting_reason_of(agent: AgentDetails) -> str | None:
    """Read the agent's ``waiting_reason`` off the AgentDetails plugin fields, if present.

    Each agent plugin's field generator publishes it under its own key --
    ``plugin.claude.waiting_reason``, ``plugin.codex.waiting_reason``,
    ``plugin.opencode.waiting_reason`` -- as a ``WaitingReason`` (PERMISSIONS /
    END_OF_TURN), populated on the online listing/observe path and absent (None)
    when the host wasn't probed online. The publishing key matches ``agent.type``,
    so read that key. The stored value may be the enum or its serialized string,
    so normalize to an upper-case string. A type with no such field simply has no
    matching block -> None.
    """
    plugin = agent.plugin if isinstance(agent.plugin, dict) else {}
    plugin_fields = plugin.get(agent.type)
    if not isinstance(plugin_fields, dict):
        return None
    raw = plugin_fields.get("waiting_reason")
    return None if raw is None else str(getattr(raw, "value", raw)).upper()


def is_permissions_blocked(agent: AgentDetails) -> bool:
    """True if mngr already knows the agent is blocked on a permission dialog.

    A free, pane-less signal (``waiting_reason == PERMISSIONS``): the
    permissions_waiting marker is set during a live turn. For claude it is OR'd
    with the pane ``❯`` rule so a permission prompt greys the composer even
    before / without a capture; for codex and opencode (which run no other blocking
    menus) it is the sole needs-input signal.
    """
    reason = waiting_reason_of(agent)
    return reason is not None and "PERMISSIONS" in reason


# The highlighted numbered option under the selection cursor: "❯ 1. Yes". Anchored
# at line start (after optional indent) so echoed transcript text mid-line can't
# trip it. Every claude choice dialog renders its active option this way.
_CHOICE_CURSOR_RE = re.compile(r"^\s*❯\s*\d+\.\s", re.MULTILINE)
# Any *non-cursor* numbered option: "  2. No". A real menu always has >=2 options,
# so requiring one of these alongside the cursor rejects a stray "❯ 1.".
_MENU_OPTION_RE = re.compile(r"^\s*\d+\.\s", re.MULTILINE)
# How many lines from the bottom of the pane to inspect (a dialog always occupies
# the bottom); scanning only the tail avoids matching old output still on screen.
_TAIL_LINES = 25

# Bound the capture-pane probe so an unresponsive host can't wedge the poll.
_HOST_COMMAND_TIMEOUT_SECONDS = 10.0
# The one generic label -- the UI shows a single "interactive prompt" state
# regardless of which dialog it is.
_GENERIC_DIALOG_REASON = "interactive prompt"


def classify_blocking_pane(content: str) -> str | None:
    """Return a reason if the pane's tail shows a numbered-choice dialog, else None.

    Pure function over a tmux pane capture -- unit-tested against fixtures. The
    reason is only a label (the UI shows one generic state); ``None`` means the
    composer is usable.
    """
    if not content:
        return None
    tail = "\n".join(content.splitlines()[-_TAIL_LINES:])
    if _CHOICE_CURSOR_RE.search(tail) and _MENU_OPTION_RE.search(tail):
        return _GENERIC_DIALOG_REASON
    return None


def detect_blocking_dialog(pool: ConnectionPool, agent_name: str) -> str | None:
    """Capture the agent's tmux pane (via the warm pool) and classify it.

    Resolves through the connection pool -- a cached, warm SSH connection -- so
    the 4s poll no longer pays mngr's ~3s discovery each time. Any failure (agent
    not found, host offline, capture error) returns None: an indeterminate probe
    must not grey a usable composer.
    """
    window = pool.mngr_ctx.config.tmux.primary_window_name

    def _capture(agent: AgentInterface, host: OnlineHostInterface) -> str | None:
        target = TmuxWindowTarget(session_name=agent.session_name, window=window)
        result = host.execute_stateful_command(
            f"tmux capture-pane -p -t {target.as_shell_arg()}",
            timeout_seconds=_HOST_COMMAND_TIMEOUT_SECONDS,
        )
        if not result.success:
            logger.trace("input-state capture-pane for {} failed: {}", agent_name, result.stderr or result.stdout)
            return None
        return classify_blocking_pane(result.stdout)

    try:
        return pool.run_on_host(agent_name, _capture)
    except Exception as e:  # noqa: BLE001 - a failed probe is "unknown", not "blocked"
        logger.trace("input-state probe for {} failed: {}", agent_name, e)
        return None
