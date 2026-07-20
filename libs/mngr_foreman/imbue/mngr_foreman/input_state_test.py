"""Tests for the blocking-dialog pane classifier."""

from __future__ import annotations

from imbue.mngr_foreman.input_state import classify_blocking_pane
from imbue.mngr_foreman.input_state import is_busy_state

# A normal, ready claude prompt (not blocked). Mirrors a real capture: a finished
# assistant turn, then an empty input row and the status bar.
_READY_PANE = """\
● Everything fired. Here's the tour:
  - demo/hello.py created and edited.
✻ Brewed for 48s
────────────────────────────────────────────────
❯
────────────────────────────────────────────────
  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents
"""

# Ready but unauthenticated: the "Not logged in · Run /login" hint sits in the
# status bar at a normal prompt -- NOT a blocking dialog (the user can type).
_NOT_LOGGED_IN_STATUS = """\
 Welcome back!
❯
────────────────────────────────────────────────
  ⏸ manual mode on · ← for agents            Not logged in · Run /login
"""


def test_ready_prompt_not_blocked() -> None:
    assert classify_blocking_pane(_READY_PANE) is None


def test_ready_with_typed_text_not_blocked() -> None:
    assert classify_blocking_pane("some output\n❯ hello there\n") is None


def test_not_logged_in_status_bar_is_not_blocked() -> None:
    # The status-bar hint must not grey the composer -- only the /login screen does.
    assert classify_blocking_pane(_NOT_LOGGED_IN_STATUS) is None


def test_empty_pane_not_blocked() -> None:
    assert classify_blocking_pane("") is None


def test_trust_dialog_detected() -> None:
    pane = "Do you trust the files in this folder?\n❯ 1. Yes, I trust this folder\n  2. No\n"
    assert classify_blocking_pane(pane) == "trust dialog"


def test_theme_selection_detected() -> None:
    pane = "Choose the text style that looks best with your terminal:\n❯ 1. Dark\n  2. Light\n"
    assert classify_blocking_pane(pane) == "theme selection dialog"


def test_cost_threshold_needs_both_strings() -> None:
    only_one = "Learn more about how to monitor your spending:\n"
    assert classify_blocking_pane(only_one) != "cost threshold dialog"
    both = "Learn more about how to monitor your spending:\nhttps://code.claude.com/docs\n"
    assert classify_blocking_pane(both) == "cost threshold dialog"


def test_login_menu_detected_as_generic_prompt() -> None:
    # The /login menu renders numbered options -> caught by the generic cursor.
    pane = "── Sign in ──\nSelect login method:\n❯ 1. Claude account with subscription\n  2. Anthropic Console\n"
    assert classify_blocking_pane(pane) == "interactive prompt"


def test_plan_approval_detected_as_generic_prompt() -> None:
    pane = "Here is my plan.\nWould you like to proceed?\n❯ 1. Yes, and auto-accept edits\n  2. No, keep planning\n"
    assert classify_blocking_pane(pane) == "interactive prompt"


def test_permission_prompt_detected_as_generic_prompt() -> None:
    pane = "Bash command\n  rm -rf build/\nDo you want to proceed?\n❯ 1. Yes\n  2. No\n"
    assert classify_blocking_pane(pane) == "interactive prompt"


def test_askuserquestion_menu_detected() -> None:
    # AskUserQuestion / model picker: numbered options under the ❯ cursor.
    pane = "Which option?\n❯ 1. Alpha\n  2. Beta\n  3. Gamma\n"
    assert classify_blocking_pane(pane) == "interactive prompt"


def test_choice_cursor_requires_number() -> None:
    # A slash command typed at the prompt ("❯ /login") is not a menu.
    assert classify_blocking_pane("output\n❯ /login\n") is None


# ---- is_busy_state: mngr's authoritative busy/idle signal for the working dot ----


def test_running_is_busy() -> None:
    # RUNNING = 'active' marker present + process alive = claude mid-turn.
    assert is_busy_state("RUNNING") is True


def test_running_is_case_insensitive() -> None:
    # AgentDetails.state may arrive lower/mixed case depending on the enum repr.
    assert is_busy_state("running") is True


def test_waiting_is_idle() -> None:
    # WAITING = END_OF_TURN (idle at prompt) or PERMISSIONS (blocked) -- not generating.
    assert is_busy_state("WAITING") is False


def test_terminal_states_are_idle() -> None:
    for state in ("STOPPED", "DONE", "REPLACED", "UNKNOWN"):
        assert is_busy_state(state) is False


def test_running_unknown_agent_type_is_not_busy() -> None:
    # Cannot arise for a claude agent (type is known); we do not treat the
    # ambiguous "process up but activity unknown" state as busy.
    assert is_busy_state("RUNNING_UNKNOWN_AGENT_TYPE") is False


def test_none_and_empty_are_idle() -> None:
    assert is_busy_state(None) is False
    assert is_busy_state("") is False
