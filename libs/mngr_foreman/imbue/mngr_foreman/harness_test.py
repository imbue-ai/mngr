"""Tests for the per-agent-type transcript strategy registry."""

from __future__ import annotations

from imbue.mngr_foreman.codex_transcript import parse_codex_common_lines
from imbue.mngr_foreman.harness import transcript_strategy_for
from imbue.mngr_foreman.pi_transcript import parse_pi_common_lines
from imbue.mngr_foreman.transcript_parser import parse_claude_session_lines


def test_claude_strategy() -> None:
    strategy = transcript_strategy_for("claude")
    assert strategy is not None
    assert strategy.subpath == "logs/claude_transcript/events.jsonl"
    assert strategy.parse is parse_claude_session_lines
    # claude gates the composer behind its numbered-choice TUI dialogs.
    assert strategy.uses_pane_dialog_detection is True


def test_pi_strategy() -> None:
    strategy = transcript_strategy_for("pi-coding")
    assert strategy is not None
    assert strategy.subpath == "events/pi-coding/common_transcript/events.jsonl"
    assert strategy.parse is parse_pi_common_lines
    # pi runs every tool unattended, so it never blocks the composer.
    assert strategy.uses_pane_dialog_detection is False


def test_codex_strategy() -> None:
    strategy = transcript_strategy_for("codex")
    assert strategy is not None
    assert strategy.subpath == "events/codex/common_transcript/events.jsonl"
    assert strategy.parse is parse_codex_common_lines
    # codex has no ❯-dialogs; a permission block promotes its state to WAITING instead.
    assert strategy.uses_pane_dialog_detection is False


def test_unknown_type_is_unsupported() -> None:
    assert transcript_strategy_for("opencode") is None
    assert transcript_strategy_for("") is None


def test_strategy_is_immutable() -> None:
    # A FrozenModel: the registry cannot be mutated through a handed-out strategy.
    strategy = transcript_strategy_for("pi-coding")
    assert strategy is not None
    try:
        strategy.subpath = "other"
    except (ValueError, TypeError, AttributeError):
        return
    raise AssertionError("TranscriptStrategy should be immutable")
