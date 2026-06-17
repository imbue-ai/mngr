"""Unit tests for the antigravity common-transcript converter (common_transcript_convert.py).

Exercises ``convert`` and its helpers directly against synthetic raw-transcript
streams on disk, without the surrounding shell script. The shell integration
(common_transcript.sh invoking this module) is covered by common_transcript_test.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record
from imbue.mngr_antigravity.resources import common_transcript_convert


def _event(
    *,
    conv_id: str,
    step_index: int,
    source: str,
    type_: str,
    content: Any = None,
    tool_calls: list[dict[str, Any]] | None = None,
    status: str = "DONE",
    timestamp: str = "2026-05-21T07:00:00Z",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "step_index": step_index,
        "source": source,
        "type": type_,
        "status": status,
        "created_at": timestamp,
        "_mngr_conv_id": conv_id,
    }
    if content is not None:
        body["content"] = content
    if tool_calls is not None:
        body["tool_calls"] = tool_calls
    return body


def _write(input_file: Path, lines: list[Any]) -> None:
    input_file.write_text("\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n")


def _events(output_file: Path) -> list[dict[str, Any]]:
    if not output_file.exists():
        return []
    return [json.loads(line) for line in output_file.read_text().splitlines() if line.strip()]


def test_user_input_becomes_user_message(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(in_f, [_event(conv_id="c1", step_index=0, source="USER_EXPLICIT", type_="USER_INPUT", content="  hi  ")])
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 1
    event = _events(out_f)[0]
    assert event["type"] == "user_message"
    assert event["content"] == "hi"
    assert event["event_id"] == "c1-0-user"
    assert validate_common_transcript_record(event) is None


def test_non_string_user_content_is_dropped(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(in_f, [_event(conv_id="c1", step_index=0, source="USER_EXPLICIT", type_="USER_INPUT", content={"x": 1})])
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 0


def test_planner_response_with_tool_call_and_code_action_pairs(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        in_f,
        [
            _event(
                conv_id="c1",
                step_index=1,
                source="MODEL",
                type_="PLANNER_RESPONSE",
                content="running a tool",
                tool_calls=[{"name": "run_command", "args": {"cmd": "ls"}}],
            ),
            _event(conv_id="c1", step_index=2, source="MODEL", type_="CODE_ACTION", content="output text"),
        ],
    )
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 2
    events = {e["type"]: e for e in _events(out_f)}
    assistant = events["assistant_message"]
    result = events["tool_result"]
    assert assistant["tool_calls"][0]["tool_name"] == "run_command"
    assert assistant["tool_calls"][0]["tool_call_id"] == "c1-1-tc0"
    # The CODE_ACTION pairs with the preceding tool call's synthetic id.
    assert result["tool_call_id"] == "c1-1-tc0"
    assert result["output"] == "output text"
    assert result["is_error"] is False


def test_code_action_error_status_sets_is_error(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        in_f,
        [
            _event(
                conv_id="c1",
                step_index=1,
                source="MODEL",
                type_="PLANNER_RESPONSE",
                tool_calls=[{"name": "run_command", "args": {}}],
            ),
            _event(conv_id="c1", step_index=2, source="MODEL", type_="CODE_ACTION", content="boom", status="ERROR"),
        ],
    )
    common_transcript_convert.convert(str(in_f), str(out_f))
    result = [e for e in _events(out_f) if e["type"] == "tool_result"][0]
    assert result["is_error"] is True


def test_code_action_with_non_string_content_is_dropped(tmp_path: Path) -> None:
    # A CODE_ACTION whose content is JSON null (key present, value null) carries no
    # usable output and would crash _truncate; it is dropped rather than emitted as
    # an empty tool_result.
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        in_f,
        [
            _event(
                conv_id="c1",
                step_index=1,
                source="MODEL",
                type_="PLANNER_RESPONSE",
                tool_calls=[{"name": "run_command", "args": {}}],
            ),
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "CODE_ACTION",
                "status": "DONE",
                "created_at": "2026-05-21T07:00:00Z",
                "_mngr_conv_id": "c1",
                "content": None,
            },
        ],
    )
    common_transcript_convert.convert(str(in_f), str(out_f))
    assert [e for e in _events(out_f) if e["type"] == "tool_result"] == []


def test_code_action_without_preceding_tool_call_is_dropped(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(in_f, [_event(conv_id="c1", step_index=2, source="MODEL", type_="CODE_ACTION", content="orphan")])
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 0


def test_unknown_source_type_is_dropped(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(in_f, [_event(conv_id="c1", step_index=0, source="SYSTEM", type_="CONVERSATION_HISTORY")])
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 0


def test_events_without_conv_id_or_step_index_are_dropped(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        in_f,
        [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "no conv id"},
            {"_mngr_conv_id": "c1", "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "no step index"},
        ],
    )
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 0


def test_dedup_against_existing_output(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(in_f, [_event(conv_id="c1", step_index=0, source="USER_EXPLICIT", type_="USER_INPUT", content="hi")])
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 1
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 0
    assert len(_events(out_f)) == 1


def test_malformed_line_is_skipped(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        in_f,
        [
            "{ not valid json",
            _event(conv_id="c1", step_index=0, source="USER_EXPLICIT", type_="USER_INPUT", content="ok"),
        ],
    )
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 1


def test_corrupt_existing_output_line_is_skipped(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    out_f.write_text("{corrupt existing line\n")
    _write(in_f, [_event(conv_id="c1", step_index=0, source="USER_EXPLICIT", type_="USER_INPUT", content="hi")])
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 1


def test_long_tool_output_is_truncated(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    long_output = "x" * (common_transcript_convert._MAX_OUTPUT_LENGTH + 100)
    _write(
        in_f,
        [
            _event(
                conv_id="c1",
                step_index=1,
                source="MODEL",
                type_="PLANNER_RESPONSE",
                tool_calls=[{"name": "run_command", "args": {}}],
            ),
            _event(conv_id="c1", step_index=2, source="MODEL", type_="CODE_ACTION", content=long_output),
        ],
    )
    common_transcript_convert.convert(str(in_f), str(out_f))
    result = [e for e in _events(out_f) if e["type"] == "tool_result"][0]
    assert result["output"].endswith("...")
    assert len(result["output"]) == common_transcript_convert._MAX_OUTPUT_LENGTH + 3


def test_missing_input_file_returns_zero(tmp_path: Path) -> None:
    assert common_transcript_convert.convert(str(tmp_path / "missing.jsonl"), str(tmp_path / "out.jsonl")) == 0


def test_non_utf8_byte_in_input_does_not_abort(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    # Raw transcript streams can carry arbitrary bytes; a single undecodable byte
    # must not abort the (append-only) conversion pass.
    valid_line = json.dumps(
        _event(conv_id="c1", step_index=0, source="USER_EXPLICIT", type_="USER_INPUT", content="real")
    ).encode()
    in_f.write_bytes(b"\xff\xfe garbage byte line\n" + valid_line + b"\n")
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 1
    assert _events(out_f)[0]["content"] == "real"
