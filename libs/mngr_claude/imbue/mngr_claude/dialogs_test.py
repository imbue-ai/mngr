"""Unit tests for the claude dialog registry. Canned panes only -- no tmux, no agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from imbue.mngr_claude.dialogs import ALL_KNOWN_AND_UNKNOWN_DIALOGS
from imbue.mngr_claude.dialogs import ALL_KNOWN_DIALOGS
from imbue.mngr_claude.dialogs import Answerable
from imbue.mngr_claude.dialogs import DIALOGS
from imbue.mngr_claude.dialogs import DialogBlocked
from imbue.mngr_claude.dialogs import EffortSwitchWarning
from imbue.mngr_claude.dialogs import EmptyShellMode
from imbue.mngr_claude.dialogs import GenericBenign
from imbue.mngr_claude.dialogs import MatchesPattern
from imbue.mngr_claude.dialogs import ModelSwitchWarning
from imbue.mngr_claude.dialogs import PendingShellCommand
from imbue.mngr_claude.dialogs import SELECTABLE_NICKNAMES
from imbue.mngr_claude.dialogs import SelfClearing
from imbue.mngr_claude.dialogs import StatusWindow
from imbue.mngr_claude.dialogs import Unrecognized
from imbue.mngr_claude.dialogs import classify
from imbue.mngr_claude.dialogs import cycle_to_option
from imbue.mngr_claude.dialogs import deal_with_dialogs
from imbue.mngr_claude.dialogs import is_nonbenign_answer_allowed
from imbue.mngr_claude.dialogs import is_option_highlighted
from imbue.mngr_claude.dialogs import is_pending_shell_command
from imbue.mngr_claude.dialogs import is_stranded_in_empty_shell_mode

_IDLE_PANE = "some assistant output\n\n❯ \n  ? for shortcuts\n"

_THEME_PANE = (
    " Choose the text style that looks best with your terminal\n"
    "\n"
    "   1. Auto (match terminal)\n"
    " ❯ 2. Dark mode\n"
    "   3. Light mode\n"
    "\n"
    " Enter to select · Esc to cancel\n"
)

_MODEL_SWITCH_PANE = (
    " Switch model?\n"
    "\n"
    " This conversation is cached for the current model. Switching to\n"
    " Opus 4.5 means the full history gets re-read on your next message.\n"
    "\n"
    " ❯ 1. Yes, switch to Opus 4.5\n"
    "   2. No\n"
)

_USAGE_LIMIT_PANE = (
    " Usage limit\n\n ❯ 1. Continue with usage credits\n   2. Stop and wait for limit to reset\n   3. Buy more\n"
)

_SHELL_PENDING_PANE = "! ls -la\n\n ! for shell mode\n"
_SHELL_EMPTY_PANE = "! \n\n ! for shell mode\n"


class FakePane:
    """A scripted pane. `frames` are returned in order by capture(); the last repeats."""

    def __init__(self, frames: list[str], accepts: frozenset[str] = frozenset()) -> None:
        self._frames = frames
        self._accepted = accepts
        self.keys: list[str] = []

    def capture(self) -> str:
        frame = self._frames[0]
        if len(self._frames) > 1:
            self._frames = self._frames[1:]
        return frame

    def press_enter(self) -> None:
        self.keys.append("Enter")

    def press_key(self, key: str) -> None:
        self.keys.append(key)

    def press_down(self) -> None:
        self.keys.append("Down")

    def accepts(self, nickname: str) -> bool:
        return nickname in self._accepted


# -- classify ---------------------------------------------------------------------------


def test_idle_pane_has_no_dialog() -> None:
    assert classify(_IDLE_PANE) is None


def test_theme_dialog_is_generic_benign() -> None:
    """The theme dialog has no rule line above its options, so only the footer finds it."""
    assert isinstance(classify(_THEME_PANE), GenericBenign)


def test_model_switch_beats_generic_benign() -> None:
    """Ordering is load-bearing: a switch warning carrying an Esc footer must NOT be Escaped."""
    pane = _MODEL_SWITCH_PANE + " Enter to select · Esc to cancel\n"
    assert isinstance(classify(pane), ModelSwitchWarning)


def test_effort_switch_is_its_own_class() -> None:
    pane = _MODEL_SWITCH_PANE.replace("current model", "current effort level")
    assert isinstance(classify(pane), EffortSwitchWarning)


def test_model_switch_matches_across_wrapped_lines() -> None:
    """capture-pane emits one line per screen row, so the pattern must not need one line."""
    assert isinstance(classify(_MODEL_SWITCH_PANE), ModelSwitchWarning)


def test_missing_prompt_with_no_match_is_unrecognized() -> None:
    assert isinstance(classify(" Something we have never seen\n\n ❯ 1. Do it\n"), Unrecognized)


def test_shell_mode_goes_through_the_same_loop() -> None:
    """Shell mode holds the input like a dialog, so it is dealt with like one."""
    assert isinstance(classify(_SHELL_PENDING_PANE), PendingShellCommand)
    assert isinstance(classify(_SHELL_EMPTY_PANE), EmptyShellMode)
    assert is_pending_shell_command(_SHELL_PENDING_PANE)
    assert is_stranded_in_empty_shell_mode(_SHELL_EMPTY_PANE)


def test_empty_shell_mode_backspaces_out() -> None:
    """Self-clearing, but with BSpace rather than Escape -- the only difference."""
    pane = FakePane([_SHELL_EMPTY_PANE])
    dialog = classify(_SHELL_EMPTY_PANE)
    assert dialog is not None
    dialog.deal_with(pane)
    assert pane.keys == ["BSpace"]


def test_pending_shell_command_refuses() -> None:
    """A human's half-typed command is theirs; mngr says so rather than submitting it."""
    pane = FakePane([_SHELL_PENDING_PANE])
    with pytest.raises(DialogBlocked) as excinfo:
        PendingShellCommand().deal_with(pane)
    assert "Enter in its terminal to run it" in excinfo.value.message
    assert pane.keys == []


# -- behaviour --------------------------------------------------------------------------


def test_benign_dialog_presses_escape() -> None:
    pane = FakePane([_THEME_PANE])
    dialog = classify(_THEME_PANE)
    assert dialog is not None
    dialog.deal_with(pane)
    assert pane.keys == ["Escape"]


_UNKNOWN_SELECTOR_PANE = " Choose a theme\n\n ❯ 1. Dark mode\n   2. Light mode\n"
_UNKNOWN_SELECTOR_MOVED = " Choose a theme\n\n   1. Dark mode\n ❯ 2. Light mode\n"


def test_unrecognized_refuses_when_not_opted_in() -> None:
    """The default. Nothing is pressed at a surface nobody has named."""
    pane = FakePane([_UNKNOWN_SELECTOR_PANE])
    with pytest.raises(DialogBlocked):
        Unrecognized().deal_with(pane)
    assert pane.keys == []


def test_unrecognized_presses_one_when_opted_in() -> None:
    pane = FakePane([_UNKNOWN_SELECTOR_PANE], accepts=frozenset({"Unrecognized"}))
    Unrecognized().deal_with(pane)
    assert pane.keys == ["1"]


def test_unrecognized_refuses_without_a_selector_even_when_opted_in() -> None:
    """A digit is only an answer where there are numbered options.

    Without this the "1" would be typed as text into whatever holds the input -- and since the
    caller loops, it would be typed once per pass.
    """
    pane = FakePane([_IDLE_PANE], accepts=frozenset({"Unrecognized"}))
    with pytest.raises(DialogBlocked):
        Unrecognized().deal_with(pane)
    assert pane.keys == []


def test_unrecognized_refuses_when_pressing_one_changes_nothing() -> None:
    """Pressing is not succeeding: an unchanged pane still refuses the send."""
    pane = FakePane([_UNKNOWN_SELECTOR_PANE, _UNKNOWN_SELECTOR_PANE], accepts=frozenset({"Unrecognized"}))
    with pytest.raises(DialogBlocked) as excinfo:
        deal_with_dialogs(pane)
    assert excinfo.value.nickname == "Unrecognized"
    assert pane.keys == ["1"]


def test_unrecognized_refuses_after_the_pass_budget() -> None:
    """A surface that keeps changing but never clears is given up on, not pressed forever."""
    # Two captures per pass: the loop's own, then deal_with's selector check. The loop's
    # captures must differ from one another or the no-progress guard ends it early instead.
    frames = [_UNKNOWN_SELECTOR_PANE, _UNKNOWN_SELECTOR_PANE, _UNKNOWN_SELECTOR_MOVED, _UNKNOWN_SELECTOR_MOVED] * 2
    pane = FakePane(frames, accepts=frozenset({"Unrecognized"}))
    with pytest.raises(DialogBlocked):
        deal_with_dialogs(pane)
    assert pane.keys == ["1", "1", "1", "1"]


def test_unknown_token_grants_only_unknown_and_known_token_never_does() -> None:
    assert is_nonbenign_answer_allowed("Unrecognized", (ALL_KNOWN_AND_UNKNOWN_DIALOGS,)) is True
    assert is_nonbenign_answer_allowed("Unrecognized", (ALL_KNOWN_DIALOGS,)) is False
    # Naming it literally does not opt in: guessing has to be asked for by the token.
    assert is_nonbenign_answer_allowed("Unrecognized", ("Unrecognized",)) is False
    # The broader token still covers everything the narrower one did.
    assert is_nonbenign_answer_allowed("Model switch warning", (ALL_KNOWN_AND_UNKNOWN_DIALOGS,)) is True


def test_accept_refuses_when_not_opted_in() -> None:
    pane = FakePane([_MODEL_SWITCH_PANE])
    with pytest.raises(DialogBlocked) as excinfo:
        ModelSwitchWarning().deal_with(pane)
    assert excinfo.value.nickname == "Model switch warning"
    assert pane.keys == []


def test_accept_presses_enter_when_already_on_the_option() -> None:
    pane = FakePane([_MODEL_SWITCH_PANE], accepts=frozenset({"Model switch warning"}))
    ModelSwitchWarning().deal_with(pane)
    assert pane.keys == ["Enter"]


def test_accept_cycles_onto_its_option_first() -> None:
    """The highlight starts on 'Continue with usage credits' -- pressing Enter there buys credits."""
    moved = _USAGE_LIMIT_PANE.replace(" ❯ 1. Continue", "   1. Continue").replace(
        "   2. Stop and wait", " ❯ 2. Stop and wait"
    )
    pane = FakePane([_USAGE_LIMIT_PANE, moved], accepts=frozenset({"Usage limit reached"}))
    dialog = classify(_USAGE_LIMIT_PANE)
    assert dialog is not None
    dialog.deal_with(pane)
    assert pane.keys == ["Down", "Enter"]


def test_accept_refuses_when_its_option_never_appears() -> None:
    """A stale label must degrade to a clean error, never to Enter on the wrong row."""
    pane = FakePane(
        [_USAGE_LIMIT_PANE.replace("Stop and wait for limit to reset", "Something else")],
        accepts=frozenset({"Usage limit reached"}),
    )
    with pytest.raises(DialogBlocked):
        DIALOGS[2].deal_with(pane)
    assert "Enter" not in pane.keys


# -- option predicate -------------------------------------------------------------------


def test_is_option_highlighted_reads_only_the_arrow_row() -> None:
    assert is_option_highlighted(_USAGE_LIMIT_PANE, "Continue with usage credits")
    assert not is_option_highlighted(_USAGE_LIMIT_PANE, "Buy more")


def test_is_option_highlighted_ignores_the_column_zero_prompt() -> None:
    """The input prompt is the same glyph; only the INDENTED form is a selector row."""
    assert not is_option_highlighted("❯ 1. not a selector\n", "not a selector")


def test_cycle_refuses_without_moving_when_the_option_is_absent() -> None:
    """Refusing must not leave the user's selector parked on an arbitrary row."""
    pane = FakePane([_USAGE_LIMIT_PANE])
    assert cycle_to_option(pane, "nonexistent option", max_steps=3) is False
    assert pane.keys == []


def test_cycle_gives_up_after_max_steps() -> None:
    """The option is on screen but the highlight never reaches it -- bounded, not a hang."""
    # The highlight never moves off "Continue with usage credits".
    pane = FakePane([_USAGE_LIMIT_PANE])
    assert cycle_to_option(pane, "Buy more", max_steps=3) is False
    assert pane.keys == ["Down", "Down", "Down"]


# -- registry invariants ----------------------------------------------------------------


def test_generic_benign_is_tried_last() -> None:
    """It matches a footer many named dialogs also carry, so it must not pre-empt them."""
    assert isinstance(DIALOGS[-1], GenericBenign)


def test_only_accept_dialogs_are_selectable() -> None:
    assert SELECTABLE_NICKNAMES == {d.get_nickname() for d in DIALOGS if isinstance(d, Answerable)}
    assert not any(isinstance(d, SelfClearing) and d.get_nickname() in SELECTABLE_NICKNAMES for d in DIALOGS)


def test_every_dialog_has_a_distinct_nickname() -> None:
    nicknames = [d.get_nickname() for d in DIALOGS]
    assert len(nicknames) == len(set(nicknames))


def test_every_accept_dialog_names_an_option() -> None:
    """None may fall back to the highlighted row -- that is what the user may have moved."""
    for dialog in DIALOGS:
        if isinstance(dialog, Answerable):
            assert dialog.get_option_label(), dialog.get_nickname()


def test_benign_dialogs_have_no_message() -> None:
    """They never raise, so a message would be dead text that could drift into being wrong."""
    for dialog in DIALOGS:
        if isinstance(dialog, SelfClearing):
            assert dialog.get_message() == ""


def test_messages_do_not_leak_config_names() -> None:
    """The modal is for the user, not the operator."""
    for dialog in (*DIALOGS, Unrecognized()):
        assert "sensibly_deal_with_dialogs" not in dialog.get_message()


# -- regressions from review ------------------------------------------------------------


def test_transcript_quoting_a_footer_is_not_a_dialog() -> None:
    """An ordinary turn whose output contains "esc to cancel" must not be Escaped.

    The pane carries the real bottom chrome claude draws around its input box. That is what
    puts the quote outside the footer region: the benign catch-all reads only the last few
    non-blank lines, and when the box is drawn those lines are the chrome, never transcript.
    """
    pane = (
        "assistant: press esc to cancel, it said\n"
        "\n"
        "────────────────────────────\n"
        "❯ \n"
        "────────────────────────────\n"
        "  ? for shortcuts\n"
    )
    assert classify(pane) is None


def test_dialog_is_found_with_the_users_own_turns_echoed_above_it() -> None:
    """The regression this whole change exists for.

    Claude echoes every past user turn with the prompt glyph at column 0, so a whole-pane
    search for that glyph reports "the input box is here" in any conversation with history.
    Measured on a live agent whose settings window was open: the send pasted into the
    window's search field because the pane was read as ready.
    """
    pane = "\n".join(
        ["❯ /theme", "  ⎿  Theme set to dark", "❯ alr", "  ⎿  ok"]
        + [
            "   Settings  Status   Config   Usage   Stats",
            "   ╭──────────╮",
            "   │ ⌕ Search settings…",
            "   ╰──────────╯",
        ]
        + [f"     Some setting {index}                    true" for index in range(18)]
        + ["   Enter/Space to change · / to search · Esc to close"]
    )
    assert isinstance(classify(pane), StatusWindow)


def test_short_dialog_is_found_with_a_turn_echoed_inside_the_input_region() -> None:
    """A dialog need not be taller than the input region.

    The `/model` switch confirmation is about seven lines, so the echo of the user's own
    `/model` turn falls INSIDE the region, directly above it. This is why the catalogue is
    walked before the input box is looked for -- asking about the box first reads that echo
    as a live prompt and misses the confirmation.
    """
    pane = "❯ /model opus\n" + _MODEL_SWITCH_PANE
    assert isinstance(classify(pane), ModelSwitchWarning)


def test_status_window_does_not_match_words_on_separate_lines() -> None:
    """The tab strip is one row, so its separator must not span newlines.

    `\\s` would, letting five bare words in a list or a --help dump match. This class is
    self-clearing, so that false positive costs a real Escape on a live turn.
    """
    pane = "Settings\nStatus\nConfig\nUsage\nStats\n────────\n❯ \n────────\n  ? for shortcuts\n"
    assert classify(pane) is None


_SHELL_HISTORY = ["❯ earlier message", "  ⎿  replied", "❯ another one", "  ⎿  replied"]


def test_shell_mode_is_found_with_a_turn_echoed_close_above_it() -> None:
    """Shell mode is recognised by the input ROW, not by whether a prompt glyph is nearby.

    In a short conversation the echo of a past user turn sits within the input region, so
    "there is a prompt line nearby" is true even while shell mode holds the input. Keying off
    that would leave the send to walk into shell mode exactly as it did before this change --
    the same defect, just needing a shorter conversation to reach.
    """
    pending = "\n".join(
        _SHELL_HISTORY + ["────────────", "!grep -r TODO .", "────────────", "  ! for shell mode · ? for shortcuts"]
    )
    assert isinstance(classify(pending), PendingShellCommand)

    strand = "\n".join(_SHELL_HISTORY + ["────────────", "!", "────────────", "  ! for shell mode · ? for shortcuts"])
    assert isinstance(classify(strand), EmptyShellMode)


def test_delivery_recovers_once_the_shell_command_is_resolved() -> None:
    """The second half of the pending-shell-command behaviour: refusal must not be sticky."""
    resolved = "\n".join(
        _SHELL_HISTORY + ["  ⎿  (cancelled)", "────────────", "❯ ", "────────────", "  ? for shortcuts"]
    )
    assert classify(resolved) is None


def test_shell_mode_is_not_claimed_from_transcript_text() -> None:
    """A conversation that merely discusses shell mode must not classify as shell mode.

    The shell entries are checked before the dialog surfaces, so a whole-pane search for the
    footer text would claim shell mode for a pane holding a settings window instead -- and
    refuse the send with an error naming a command that does not exist.
    """
    pane = "\n".join(
        ["❯ how does ! for shell mode work?", "  ⎿  explained"]
        + ["   Settings  Status   Config   Usage   Stats"]
        + [f"     Some setting {index}   true" for index in range(10)]
        + ["   Enter/Space to change · / to search · Esc to close"]
    )
    assert isinstance(classify(pane), StatusWindow)


def test_generic_benign_is_case_insensitive() -> None:
    """claude ships lowercase variants too -- 14 "esc to cancel", 3 "esc to close"."""
    assert isinstance(classify(" a dialog\n\n ❯ 1. ok\n\n esc to cancel\n"), GenericBenign)


def test_switch_warning_matches_when_the_row_wraps() -> None:
    """capture-pane emits one line per screen row, so the sentence arrives split."""
    wrapped = " This conversation is cached for the current\n model. Switching to Opus\n\n ❯ 1. Yes, switch to Opus\n"
    assert isinstance(classify(wrapped), ModelSwitchWarning)


def test_status_window_does_not_match_scattered_words() -> None:
    """The tab strip is one row; matching across lines fires on unrelated transcript."""
    scattered = " Settings are here\n Status: ok\n Config loaded\n Usage high\n Stats shown\n\n ❯ 1. go\n"
    assert not isinstance(classify(scattered), StatusWindow)


def test_option_highlight_reads_the_last_arrow_row() -> None:
    """A stale selector scrolled above the live one must not be read as the highlight."""
    pane = " ❯ 1. Stale row\n ...\n ❯ 1. Live row\n"
    assert is_option_highlighted(pane, "Live row")
    assert not is_option_highlighted(pane, "Stale row")


# -- drift ------------------------------------------------------------------------------

_CLAUDE_BINARY = Path("/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe")

# Patterns whose text claude assembles at runtime, so no single literal appears in the
# binary. Checked by the live-agent release tests instead.
# Patterns whose text claude assembles at runtime from fragments, so no single literal
# appears in the binary. The switch warnings interpolate the model/effort name mid-sentence
# ("...for the current ", oPm, ". Switching to ", X, ...) and the status tab strip is laid
# out per tab. Covered by the live-agent release tests instead.
_ASSEMBLED_AT_RUNTIME = frozenset({"Status window", "Model switch warning", "Effort switch warning"})


@pytest.mark.skipif(not _CLAUDE_BINARY.exists(), reason="claude binary not installed")
def test_patterns_match_installed_binary() -> None:
    """Every pattern must still find its text in the shipped claude.

    This is the drift guard the module docstring promises. It exists because
    ``EffortCalloutIndicator`` matched a string absent from every version this repo has ever
    shipped and nothing noticed across two pins.
    """
    blob = _CLAUDE_BINARY.read_bytes()
    for dialog in DIALOGS:
        if not isinstance(dialog, MatchesPattern) or dialog.get_nickname() in _ASSEMBLED_AT_RUNTIME:
            continue
        # The runtime patterns are whitespace-tolerant for wrapped panes; the binary holds
        # the unwrapped literal, so a plain-space form is what to look for here.
        literal = dialog.get_pattern().pattern.replace(r"\s+", " ")
        probe = literal.split("|")[0].replace(r"\?", "?").replace("(?:", "").replace(")", "")
        assert probe.encode() in blob, f"{dialog.get_nickname()}: {probe!r} not in binary"


@pytest.mark.skipif(not _CLAUDE_BINARY.exists(), reason="claude binary not installed")
def test_option_labels_match_installed_binary() -> None:
    """A stale option label refuses the send rather than answering it, so pin them too."""
    blob = _CLAUDE_BINARY.read_bytes()
    for dialog in DIALOGS:
        if isinstance(dialog, Answerable):
            label = dialog.get_option_label()
            assert label.encode() in blob, f"{dialog.get_nickname()}: option {label!r} not in binary"
