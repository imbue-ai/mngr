"""Tests for the per-agent-type transcript/status strategy registry."""

from __future__ import annotations

from imbue.mngr_foreman.common_transcript_parser import parse_common_transcript_lines
from imbue.mngr_foreman.harness import transcript_strategy_for
from imbue.mngr_foreman.transcript_parser import parse_claude_session_lines


def test_claude_uses_raw_parser_and_pane_detection() -> None:
    strategy = transcript_strategy_for("claude")
    assert strategy is not None
    assert strategy.subpath == "logs/claude_transcript/events.jsonl"
    assert strategy.parse is parse_claude_session_lines
    assert strategy.uses_pane_dialog_detection is True


def test_opencode_uses_common_transcript_without_pane_detection() -> None:
    strategy = transcript_strategy_for("opencode")
    assert strategy is not None
    assert strategy.subpath == "events/opencode/common_transcript/events.jsonl"
    assert strategy.parse is parse_common_transcript_lines
    # opencode surfaces permission blocks via the waiting_reason field, not a pane.
    assert strategy.uses_pane_dialog_detection is False


def test_unknown_agent_type_has_no_strategy() -> None:
    assert transcript_strategy_for("codex") is None
    assert transcript_strategy_for("antigravity") is None
    assert transcript_strategy_for("") is None
