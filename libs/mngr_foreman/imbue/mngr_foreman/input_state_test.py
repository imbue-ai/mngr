"""Tests for the blocking-dialog pane classifier and the busy/idle signals."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import cast

from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr_foreman.connection_pool import ConnectionPool
from imbue.mngr_foreman.input_state import PaneStateCache
from imbue.mngr_foreman.input_state import classify_blocking_pane
from imbue.mngr_foreman.input_state import is_busy_state
from imbue.mngr_foreman.input_state import is_permissions_blocked
from imbue.mngr_foreman.input_state import waiting_reason_of


def _agent_with_plugin(plugin: object, agent_type: str = "claude") -> AgentDetails:
    """A stand-in exposing ``.plugin`` + ``.type`` (all the state helpers touch)."""
    return cast(AgentDetails, SimpleNamespace(plugin=plugin, type=agent_type))


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
    # The former per-dialog patterns are gone; the trust dialog is a numbered menu
    # like any other, caught by the single ❯ rule as the one generic label.
    pane = "Do you trust the files in this folder?\n❯ 1. Yes, I trust this folder\n  2. No\n"
    assert classify_blocking_pane(pane) == "interactive prompt"


def test_theme_selection_detected() -> None:
    pane = "Choose the text style that looks best with your terminal:\n❯ 1. Dark\n  2. Light\n"
    assert classify_blocking_pane(pane) == "interactive prompt"


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


def test_lone_cursor_without_sibling_option_not_blocked() -> None:
    # A stray "❯ 1." with no sibling numbered option (e.g. a fragment echoed in
    # output) is not a menu -- a real dialog always renders >=2 options.
    assert classify_blocking_pane("some log line\n❯ 1. only one option\n") is None


def test_cursor_must_be_at_line_start() -> None:
    # "❯ 1." embedded mid-line in echoed transcript content must not fire.
    pane = "the assistant wrote: choose ❯ 1. Alpha or 2. Beta inline\nmore text\n"
    assert classify_blocking_pane(pane) is None


def test_numbered_list_in_output_without_cursor_not_blocked() -> None:
    # Plain numbered lists in assistant output have options but no ❯ cursor.
    pane = "Here are the steps:\n1. First\n2. Second\n3. Third\n❯\n"
    assert classify_blocking_pane(pane) is None


def test_indented_menu_detected() -> None:
    # tmux panes often left-pad dialog content; the anchor allows leading indent.
    pane = "  Select an option:\n  ❯ 1. Alpha\n    2. Beta\n"
    assert classify_blocking_pane(pane) == "interactive prompt"


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


# ---- waiting_reason: the free, pane-less PERMISSIONS signal off AgentDetails ----


def test_permissions_waiting_reason_is_blocked() -> None:
    agent = _agent_with_plugin({"claude": {"waiting_reason": "PERMISSIONS"}})
    assert is_permissions_blocked(agent) is True


def test_permissions_waiting_reason_enum_value_is_blocked() -> None:
    # The stored value may be the enum rather than a plain string.
    reason = SimpleNamespace(value="PERMISSIONS")
    agent = _agent_with_plugin({"claude": {"waiting_reason": reason}})
    assert is_permissions_blocked(agent) is True


def test_end_of_turn_reason_is_not_blocked() -> None:
    agent = _agent_with_plugin({"claude": {"waiting_reason": "END_OF_TURN"}})
    assert waiting_reason_of(agent) == "END_OF_TURN"
    assert is_permissions_blocked(agent) is False


def test_missing_waiting_reason_is_not_blocked() -> None:
    # No plugin block for the agent's type, empty block, or absent field -> unknown.
    assert is_permissions_blocked(_agent_with_plugin({})) is False
    assert is_permissions_blocked(_agent_with_plugin({"claude": {}})) is False
    # A claude-typed agent ignores another type's block.
    assert is_permissions_blocked(_agent_with_plugin({"opencode": {"waiting_reason": "PERMISSIONS"}})) is False
    assert waiting_reason_of(_agent_with_plugin({})) is None


def test_waiting_reason_reads_the_agents_own_type_key() -> None:
    # codex and opencode publish waiting_reason under their own plugin key; the
    # helper keys off ``agent.type`` so each one's permission block is surfaced
    # pane-lessly, exactly like claude's.
    for agent_type in ("codex", "opencode"):
        blocked = _agent_with_plugin({agent_type: {"waiting_reason": "PERMISSIONS"}}, agent_type=agent_type)
        assert waiting_reason_of(blocked) == "PERMISSIONS"
        assert is_permissions_blocked(blocked) is True
        idle = _agent_with_plugin({agent_type: {"waiting_reason": "END_OF_TURN"}}, agent_type=agent_type)
        assert waiting_reason_of(idle) == "END_OF_TURN"
        assert is_permissions_blocked(idle) is False


def test_non_dict_plugin_is_safe() -> None:
    # Defensive: a malformed plugin payload must not raise, just read as unknown.
    assert waiting_reason_of(_agent_with_plugin(None)) is None
    assert is_permissions_blocked(_agent_with_plugin("oops")) is False


def test_pane_state_cache_single_flight() -> None:
    # Five concurrent callers for one agent must collapse to a SINGLE probe (the
    # amplification fix): the winner runs the SSH probe, the rest read its cached value.
    calls = {"n": 0}
    started = threading.Event()
    release = threading.Event()

    def fake_probe(_pool: ConnectionPool, _name: str) -> tuple[bool | None, str | None]:
        calls["n"] += 1
        started.set()
        release.wait(2.0)  # hold the flight open so the others pile onto the lock
        return (True, None)

    cache = PaneStateCache(ttl_seconds=10.0, probe_fn=fake_probe)
    pool = cast(ConnectionPool, None)
    out: list[object] = []
    threads = [threading.Thread(target=lambda: out.append(cache.probe(pool, "a"))) for _ in range(5)]
    for t in threads:
        t.start()
    assert started.wait(2.0)
    release.set()
    for t in threads:
        t.join(2.0)
    assert calls["n"] == 1  # 5 concurrent callers -> 1 SSH probe
    assert out == [(True, None)] * 5


def test_pane_state_cache_ttl_expires() -> None:
    # A fresh call after the TTL lapses re-probes (a single tab still reads live ~1s).
    calls = {"n": 0}

    def fake_probe(_pool: ConnectionPool, _name: str) -> tuple[bool | None, str | None]:
        calls["n"] += 1
        return (calls["n"] == 1, None)

    cache = PaneStateCache(ttl_seconds=0.0, probe_fn=fake_probe)  # every entry already expired
    pool = cast(ConnectionPool, None)
    cache.probe(pool, "a")
    cache.probe(pool, "a")
    assert calls["n"] == 2
