"""Unit tests for the codex common-transcript converter (common_transcript_convert.py).

Exercises ``convert`` and its helpers directly against synthetic codex rollout
streams on disk, without the surrounding shell script. The shell integration
(common_transcript.sh invoking this module) is covered by common_transcript_test.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record
from imbue.mngr_codex.resources import common_transcript_convert


def _line(type_: str, payload: dict[str, Any], timestamp: str = "2026-06-09T07:00:00.000Z") -> dict[str, Any]:
    return {"timestamp": timestamp, "type": type_, "payload": payload}


def _user(text: str) -> dict[str, Any]:
    return _line(
        "response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
    )


def _assistant(text: str) -> dict[str, Any]:
    return _line(
        "response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    )


def _function_call(name: str, arguments: str, call_id: str) -> dict[str, Any]:
    return _line("response_item", {"type": "function_call", "name": name, "arguments": arguments, "call_id": call_id})


def _function_call_output(call_id: str, output: Any) -> dict[str, Any]:
    return _line("response_item", {"type": "function_call_output", "call_id": call_id, "output": output})


def _write(input_file: Path, lines: list[Any]) -> None:
    input_file.write_text("\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n")


def _events(output_file: Path) -> list[dict[str, Any]]:
    if not output_file.exists():
        return []
    return [json.loads(line) for line in output_file.read_text().splitlines() if line.strip()]


def test_converts_user_and_assistant_messages(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_user("hello"), _assistant("hi back")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 2
    events = _events(output_file)
    assert events[0] == {
        "timestamp": "2026-06-09T07:00:00.000Z",
        "type": "user_message",
        "event_id": "line-1-user",
        "source": "codex/common_transcript",
        "role": "user",
        "content": "hello",
    }
    assert events[1]["type"] == "assistant_message"
    assert events[1]["text"] == "hi back"
    for event in events:
        assert validate_common_transcript_record(event) is None


def test_function_call_and_output_pair_into_tool_result(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [_function_call("shell", '{"cmd":"ls"}', "call-1"), _function_call_output("call-1", "file-a\nfile-b")],
    )
    common_transcript_convert.convert(str(input_file), str(output_file))
    results = [e for e in _events(output_file) if e["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["tool_name"] == "shell"
    assert results[0]["output"] == "file-a\nfile-b"


def test_function_call_output_content_array_is_stringified(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [
            _function_call("shell", "{}", "call-1"),
            _function_call_output(
                "call-1", [{"type": "output_text", "text": "part-a"}, {"type": "output_text", "text": "part-b"}]
            ),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file))
    result = [e for e in _events(output_file) if e["type"] == "tool_result"][0]
    assert result["output"] == "part-apart-b"


def test_function_call_output_without_matching_call_is_dropped(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_function_call_output("orphan", "nope")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 0


def test_event_msg_and_bookkeeping_are_ignored(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [
            _line("event_msg", {"type": "user_message", "message": "dup", "images": []}),
            _line("session_meta", {"id": "s1"}),
            _user("real"),
        ],
    )
    events = _events(output_file) if common_transcript_convert.convert(str(input_file), str(output_file)) else []
    assert [e["type"] for e in events] == ["user_message"]


def test_empty_user_message_is_dropped(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_user("")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 0


def test_dedup_against_existing_output(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_user("hello")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 1
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 0
    assert len(_events(output_file)) == 1


def test_malformed_line_is_skipped(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, ["{ not valid json", _user("after the broken line")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 1
    assert _events(output_file)[0]["content"] == "after the broken line"


def test_corrupt_existing_output_line_is_skipped(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    output_file.write_text("{corrupt existing line\n")
    _write(input_file, [_user("real")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 1


def test_missing_input_file_returns_zero(tmp_path: Path) -> None:
    assert common_transcript_convert.convert(str(tmp_path / "missing.jsonl"), str(tmp_path / "out.jsonl")) == 0


def test_long_tool_output_is_truncated(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    long_output = "x" * (common_transcript_convert._MAX_OUTPUT_LENGTH + 100)
    _write(input_file, [_function_call("shell", "{}", "call-1"), _function_call_output("call-1", long_output)])
    common_transcript_convert.convert(str(input_file), str(output_file))
    result = [e for e in _events(output_file) if e["type"] == "tool_result"][0]
    assert result["output"].endswith("...")
    assert len(result["output"]) == common_transcript_convert._MAX_OUTPUT_LENGTH + 3
