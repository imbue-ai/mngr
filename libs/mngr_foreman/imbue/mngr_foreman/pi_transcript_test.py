"""Tests for the pi-coding common-transcript normalizer."""

from __future__ import annotations

import json
from typing import Any

from imbue.mngr_foreman.pi_transcript import parse_pi_common_lines


def _line(**kwargs: Any) -> str:
    return json.dumps(kwargs)


def _assistant(event_id: str, ts: str, text: str = "ok", finish_reason: str = "end_turn") -> str:
    return _line(
        timestamp=ts,
        type="assistant_message",
        event_id=event_id,
        source="pi-coding/common_transcript",
        role="assistant",
        model="anthropic/claude",
        text=text,
        tool_calls=[{"tool_call_id": "c1", "tool_name": "bash", "input_preview": '{"command":"ls"}'}],
        finish_reason=finish_reason,
    )


def _user(event_id: str, ts: str, content: str) -> str:
    return _line(
        timestamp=ts,
        type="user_message",
        event_id=event_id,
        source="pi-coding/common_transcript",
        role="user",
        content=content,
    )


def _tool_result(event_id: str, ts: str, output: str, is_error: bool = False) -> str:
    return _line(
        timestamp=ts,
        type="tool_result",
        event_id=event_id,
        source="pi-coding/common_transcript",
        tool_call_id="c1",
        tool_name="bash",
        output=output,
        is_error=is_error,
    )


def test_user_assistant_tool_result_pass_through() -> None:
    events = parse_pi_common_lines(
        [
            _user("pi-0", "2026-01-01T00:00:00Z", "hello"),
            _assistant("pi-1", "2026-01-01T00:00:01Z", "hi back"),
            _tool_result("pi-2", "2026-01-01T00:00:02Z", "file.txt"),
        ]
    )
    assert [e["type"] for e in events] == ["user_message", "assistant_message", "tool_result"]
    assert events[0]["content"] == "hello"
    assert events[1]["text"] == "hi back"
    # The tool call's preview survives untouched (there is no input_full for pi).
    assert events[1]["tool_calls"][0]["tool_name"] == "bash"
    assert events[2]["tool_call_id"] == "c1"
    assert events[2]["output"] == "file.txt"
    assert events[2]["is_error"] is False


def test_finish_reason_renamed_to_stop_reason() -> None:
    (event,) = parse_pi_common_lines([_assistant("pi-0", "2026-01-01T00:00:00Z", finish_reason="max_tokens")])
    assert event["stop_reason"] == "max_tokens"
    assert "finish_reason" not in event


def test_dedup_on_event_id_across_calls() -> None:
    ids: set[str] = set()
    line = _user("pi-0", "2026-01-01T00:00:00Z", "once")
    first = parse_pi_common_lines([line], existing_event_ids=ids)
    second = parse_pi_common_lines([line], existing_event_ids=ids)
    assert len(first) == 1
    assert second == []


def test_events_sorted_by_timestamp() -> None:
    events = parse_pi_common_lines(
        [
            _user("pi-1", "2026-01-01T00:00:02Z", "second"),
            _user("pi-0", "2026-01-01T00:00:01Z", "first"),
        ]
    )
    assert [e["content"] for e in events] == ["first", "second"]


def test_malformed_and_blank_lines_skipped() -> None:
    events = parse_pi_common_lines(["", "not json", _user("pi-0", "2026-01-01T00:00:00Z", "kept")])
    assert [e["content"] for e in events] == ["kept"]


def test_record_without_event_id_dropped() -> None:
    no_id = _line(timestamp="2026-01-01T00:00:00Z", type="user_message", role="user", content="orphan")
    assert parse_pi_common_lines([no_id]) == []


def test_tool_output_recapped_when_stricter() -> None:
    (event,) = parse_pi_common_lines(
        [_tool_result("pi-0", "2026-01-01T00:00:00Z", "x" * 100)], max_tool_output_chars=10
    )
    assert event["output"] == "x" * 10 + "..."


def test_tool_output_uncapped_when_zero() -> None:
    (event,) = parse_pi_common_lines(
        [_tool_result("pi-0", "2026-01-01T00:00:00Z", "y" * 100)], max_tool_output_chars=0
    )
    assert event["output"] == "y" * 100


def test_tool_name_by_call_id_argument_ignored() -> None:
    # Accepted for call-site parity with the claude parser; must not affect output.
    events = parse_pi_common_lines(
        [_tool_result("pi-0", "2026-01-01T00:00:00Z", "out")], tool_name_by_call_id={"c1": "override"}
    )
    assert events[0]["tool_name"] == "bash"
