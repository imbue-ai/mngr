"""Detect whether an agent's claude TUI is showing a blocking interactive dialog.

The chat composer sends a message by pasting into the agent's tmux input box.
While claude is showing a blocking dialog (trust, permission, plan approval,
AskUserQuestion, the /login flow, ...) that paste can't be delivered -- mngr's
own send path raises ``DialogDetectedError`` in exactly this case. So the chat
page polls this detector and greys the composer when a dialog is up.

**What the detector keys on** (investigated in mngr's send preflight,
``mngr_claude/plugin.py`` ``_preflight_send_message`` + ``_DIALOG_INDICATORS``):
mngr's built-in pane detection is *narrow* -- plain substring matches for five
onboarding/cost dialogs (trust "Yes, I trust this folder", custom-API-key,
theme-selection, effort-callout, cost-threshold) plus a ``permissions_waiting``
hook file for permission prompts. It does NOT detect the /login screen,
AskUserQuestion, or plan approval.

We only need a single boolean "is any blocking dialog up", so the check is
deliberately minimal: reuse mngr's five indicators verbatim (imported, free,
and they yield a nice label) plus one generic signal -- the numbered-choice
selection cursor ``❯ 1.``. Every claude choice dialog (permission, trust, plan,
AskUserQuestion, the login menu, the model picker) renders its highlighted
option that way, so the one cursor pattern covers them all. The chat page shows
a single generic "interactive prompt" state regardless of which fired.

Detection is a single ``tmux capture-pane`` over the agent's host per poll; the
page polls lazily (only while visible + agent running), which the team approved.
"""

from __future__ import annotations

import re

from loguru import logger

from imbue.mngr.api.address_parsers import parse_agent_address
from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.find import resolve_to_started_host_and_agent
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr_claude.plugin import CostThresholdDialogIndicator
from imbue.mngr_claude.plugin import CustomApiKeyDialogIndicator
from imbue.mngr_claude.plugin import EffortCalloutIndicator
from imbue.mngr_claude.plugin import ThemeSelectionIndicator
from imbue.mngr_claude.plugin import TrustDialogIndicator

# mngr's own pane indicators, reused verbatim so we stay in sync with its send path.
_MNGR_DIALOG_INDICATORS = (
    TrustDialogIndicator(),
    CustomApiKeyDialogIndicator(),
    ThemeSelectionIndicator(),
    EffortCalloutIndicator(),
    CostThresholdDialogIndicator(),
)

# A numbered menu option under the selection cursor: "❯ 1. Yes". Every claude
# choice dialog (permission, trust, plan, AskUserQuestion, the login menu, the
# model picker) renders its highlighted option this way. The ready input row is
# "❯ " (empty) or "❯ <typed text>" -- never "❯ <digit>." -- so this does not
# fire on the normal prompt.
_CHOICE_CURSOR_RE = re.compile(r"❯\s*\d+\.")

# How many lines from the bottom of the pane to inspect for the choice cursor
# (a dialog always occupies the bottom); scanning only the tail avoids false
# positives from old assistant text still on screen.
_TAIL_LINES = 25


def classify_blocking_pane(content: str) -> str | None:
    """Return a short reason if the pane shows a blocking dialog, else None.

    Pure function over a tmux pane capture -- unit-tested against fixtures. The
    reason is only a label (the UI shows one generic state); ``None`` means the
    composer is usable.
    """
    if not content:
        return None

    # mngr's onboarding/cost dialogs fill the screen -- match against all of it.
    for indicator in _MNGR_DIALOG_INDICATORS:
        if indicator.matches(content):
            return indicator.get_description()

    # Any numbered-choice menu at the bottom of the pane is a blocking dialog.
    tail = "\n".join(content.splitlines()[-_TAIL_LINES:])
    if _CHOICE_CURSOR_RE.search(tail):
        return "interactive prompt"
    return None


def detect_blocking_dialog(mngr_ctx: MngrContext, agent_name: str) -> str | None:
    """Capture the agent's tmux pane and classify it. Returns a reason or None.

    Resolves the agent to its live host without auto-starting it. Any failure
    (agent not found, host offline, capture error) returns None -- an
    indeterminate probe must not grey a usable composer.
    """
    try:
        address = parse_agent_address(agent_name)
        host_ref, agent_ref = find_one_agent(address, mngr_ctx)
        agent, host = resolve_to_started_host_and_agent(
            host_ref=host_ref,
            agent_ref=agent_ref,
            allow_auto_start=False,
            mngr_ctx=mngr_ctx,
        )
        target = TmuxWindowTarget(
            session_name=agent.session_name,
            window=mngr_ctx.config.tmux.primary_window_name,
        )
        result = host.execute_stateful_command(f"tmux capture-pane -p -t {target.as_shell_arg()}")
    except Exception as e:  # noqa: BLE001 - a failed probe is "unknown", not "blocked"
        logger.trace("input-state probe for {} failed: {}", agent_name, e)
        return None

    if not result.success:
        logger.trace("input-state capture-pane for {} failed: {}", agent_name, result.stderr or result.stdout)
        return None
    return classify_blocking_pane(result.stdout)
