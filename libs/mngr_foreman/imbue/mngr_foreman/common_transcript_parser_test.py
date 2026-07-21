"""Tests for the common-transcript normalizer (codex / opencode / pi-coding)."""

from __future__ import annotations

import json
from typing import Any

from imbue.mngr_foreman.common_transcript_parser import parse_common_transcript_lines


def _line(record: dict[str, Any]) -> str:
    return json.dumps(record)


_USER = {
    "type": "user_message",
    "timestamp": "2026-07-20T00:00:01Z",
    "event_id": "cx-1",
    "source": "codex/common_transcript",
    "role": "user",
    "content": "fix the bug",
}
_ASSISTANT = {
    "type": "assistant_message",
    "timestamp": "2026-07-20T00:00:02Z",
    "event_id": "cx-2",
    "source": "codex/common_transcript",
    "role": "assistant",
    "model": "gpt-5.5",
    "text": "On it.",
    "tool_calls": [{"tool_call_id": "call-1", "tool_name": "shell", "input_preview": "ls -la"}],
    "parts": [
        {"type": "text", "content": "On it."},
        {"type": "tool_call", "tool_call_id": "call-1", "tool_name": "shell", "input_preview": "ls -la"},
    ],
    "parts_ordered": True,
    "finish_reason": "tool_use",
    "usage": {"input_tokens": 10, "output_tokens": 3},
}
_TOOL_RESULT = {
    "type": "tool_result",
    "timestamp": "2026-07-20T00:00:03Z",
    "event_id": "cx-3",
    "source": "codex/common_transcript",
    "tool_call_id": "call-1",
    "tool_name": "shell",
    "output": "a.py\nb.py",
    "is_error": False,
}


def test_passes_through_the_three_record_types() -> None:
    events = parse_common_transcript_lines([_line(_USER), _line(_ASSISTANT), _line(_TOOL_RESULT)])
    assert [e["type"] for e in events] == ["user_message", "assistant_message", "tool_result"]
    assert events[0]["content"] == "fix the bug"
    assert events[2]["tool_name"] == "shell" and events[2]["output"] == "a.py\nb.py"


def test_finish_reason_renamed_to_stop_reason() -> None:
    (event,) = parse_common_transcript_lines([_line(_ASSISTANT)])
    assert event["stop_reason"] == "tool_use"
    assert "finish_reason" not in event
    # The flat text + tool_calls the frontend renders survive untouched.
    assert event["text"] == "On it."
    assert event["tool_calls"][0]["input_preview"] == "ls -la"


def test_assistant_without_finish_reason_gets_null_stop_reason() -> None:
    # pi-coding omits finish_reason; the normalized event still carries the key.
    record = {**_ASSISTANT}
    del record["finish_reason"]
    (event,) = parse_common_transcript_lines([_line(record)])
    assert event["stop_reason"] is None


def test_dedup_on_event_id_across_calls() -> None:
    seen: set[str] = set()
    first = parse_common_transcript_lines([_line(_USER)], existing_event_ids=seen)
    assert len(first) == 1 and seen == {"cx-1"}
    # Re-presenting the same line (rotation / backfill overlap) emits nothing.
    second = parse_common_transcript_lines([_line(_USER)], existing_event_ids=seen)
    assert second == []


def test_events_sorted_by_timestamp() -> None:
    events = parse_common_transcript_lines([_line(_TOOL_RESULT), _line(_USER), _line(_ASSISTANT)])
    assert [e["event_id"] for e in events] == ["cx-1", "cx-2", "cx-3"]


def test_tool_result_output_recapped() -> None:
    record = {**_TOOL_RESULT, "output": "x" * 100}
    (event,) = parse_common_transcript_lines([_line(record)], max_tool_output_chars=10)
    assert event["output"] == "x" * 10 + "..."


def test_malformed_and_unknown_lines_skipped() -> None:
    lines = [
        "not json at all",
        # unknown record type, then missing event_id, then missing timestamp.
        _line({"type": "session_meta", "event_id": "m-1", "timestamp": "t"}),
        _line({"type": "user_message", "timestamp": "t"}),
        _line({"type": "user_message", "event_id": "u-9"}),
        "",
        _line(_USER),
    ]
    events = parse_common_transcript_lines(lines)
    assert [e["event_id"] for e in events] == ["cx-1"]


def test_tool_name_by_call_id_accepted_but_ignored() -> None:
    # Signature parity with the claude parser; common records are self-describing.
    mapping: dict[str, str] = {}
    events = parse_common_transcript_lines([_line(_TOOL_RESULT)], tool_name_by_call_id=mapping)
    assert events[0]["tool_name"] == "shell"
    assert mapping == {}
