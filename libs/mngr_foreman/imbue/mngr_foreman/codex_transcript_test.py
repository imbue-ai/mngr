"""Tests for the codex common-transcript normalizer."""

from __future__ import annotations

import json
from typing import Any

from imbue.mngr_foreman.codex_transcript import parse_codex_common_lines


def _line(**kwargs: Any) -> str:
    return json.dumps(kwargs)


def _assistant(event_id: str, ts: str, text: str = "ok", finish_reason: Any = None) -> str:
    # codex assistant messages carry model/usage=None and a single tool_call per turn.
    return _line(
        timestamp=ts,
        type="assistant_message",
        event_id=event_id,
        source="codex/common_transcript",
        role="assistant",
        model=None,
        text=text,
        tool_calls=[{"tool_call_id": "line-3-tc", "tool_name": "shell", "input_preview": '{"command":"ls"}'}],
        parts=[{"type": "text", "content": text}],
        parts_ordered=True,
        finish_reason=finish_reason,
        usage=None,
    )


def _user(event_id: str, ts: str, content: str) -> str:
    return _line(
        timestamp=ts,
        type="user_message",
        event_id=event_id,
        source="codex/common_transcript",
        role="user",
        content=content,
    )


def _tool_result(event_id: str, ts: str, output: str, is_error: bool = False) -> str:
    return _line(
        timestamp=ts,
        type="tool_result",
        event_id=event_id,
        source="codex/common_transcript",
        tool_call_id="line-3-tc",
        tool_name="shell",
        output=output,
        is_error=is_error,
    )


def test_user_assistant_tool_result_pass_through() -> None:
    events = parse_codex_common_lines(
        [
            _user("line-1-user", "2026-01-01T00:00:00Z", "hello"),
            _assistant("line-2-assistant", "2026-01-01T00:00:01Z", "hi back"),
            _tool_result("line-4-tool_result", "2026-01-01T00:00:02Z", "file.txt"),
        ]
    )
    assert [e["type"] for e in events] == ["user_message", "assistant_message", "tool_result"]
    assert events[0]["content"] == "hello"
    assert events[1]["text"] == "hi back"
    # The tool call's preview survives untouched (there is no input_full for codex).
    assert events[1]["tool_calls"][0]["tool_name"] == "shell"
    assert events[2]["tool_call_id"] == "line-3-tc"
    assert events[2]["output"] == "file.txt"
    assert events[2]["is_error"] is False


def test_codex_null_model_and_usage_preserved() -> None:
    # codex's converter never populates model/usage; the pass-through must keep them.
    (event,) = parse_codex_common_lines([_assistant("line-2-assistant", "2026-01-01T00:00:00Z")])
    assert event["model"] is None
    assert event["usage"] is None


def test_finish_reason_renamed_to_stop_reason() -> None:
    (event,) = parse_codex_common_lines(
        [_assistant("line-2-assistant", "2026-01-01T00:00:00Z", finish_reason="max_tokens")]
    )
    assert event["stop_reason"] == "max_tokens"
    assert "finish_reason" not in event


def test_null_finish_reason_still_renamed() -> None:
    # codex always emits finish_reason=None; it should surface as stop_reason=None.
    (event,) = parse_codex_common_lines([_assistant("line-2-assistant", "2026-01-01T00:00:00Z")])
    assert event["stop_reason"] is None
    assert "finish_reason" not in event


def test_dedup_on_event_id_across_calls() -> None:
    ids: set[str] = set()
    line = _user("line-1-user", "2026-01-01T00:00:00Z", "once")
    first = parse_codex_common_lines([line], existing_event_ids=ids)
    second = parse_codex_common_lines([line], existing_event_ids=ids)
    assert len(first) == 1
    assert second == []


def test_events_sorted_by_timestamp() -> None:
    events = parse_codex_common_lines(
        [
            _user("line-3-user", "2026-01-01T00:00:02Z", "second"),
            _user("line-1-user", "2026-01-01T00:00:01Z", "first"),
        ]
    )
    assert [e["content"] for e in events] == ["first", "second"]


def test_malformed_and_blank_lines_skipped() -> None:
    events = parse_codex_common_lines(["", "not json", _user("line-1-user", "2026-01-01T00:00:00Z", "kept")])
    assert [e["content"] for e in events] == ["kept"]


def test_record_without_event_id_dropped() -> None:
    no_id = _line(timestamp="2026-01-01T00:00:00Z", type="user_message", role="user", content="orphan")
    assert parse_codex_common_lines([no_id]) == []


def test_tool_output_recapped_when_stricter() -> None:
    (event,) = parse_codex_common_lines(
        [_tool_result("line-4-tool_result", "2026-01-01T00:00:00Z", "x" * 100)], max_tool_output_chars=10
    )
    assert event["output"] == "x" * 10 + "..."


def test_tool_output_uncapped_when_zero() -> None:
    (event,) = parse_codex_common_lines(
        [_tool_result("line-4-tool_result", "2026-01-01T00:00:00Z", "y" * 100)], max_tool_output_chars=0
    )
    assert event["output"] == "y" * 100


def test_tool_name_by_call_id_argument_ignored() -> None:
    # Accepted for call-site parity with the claude parser; must not affect output.
    events = parse_codex_common_lines(
        [_tool_result("line-4-tool_result", "2026-01-01T00:00:00Z", "out")],
        tool_name_by_call_id={"line-3-tc": "override"},
    )
    assert events[0]["tool_name"] == "shell"
