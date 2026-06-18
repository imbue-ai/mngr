"""Unit tests for the claude common-transcript converter (common_transcript_convert.py).

Exercises ``convert`` and its helpers directly against synthetic raw-transcript
streams on disk, without the surrounding shell script. The shell integration
(common_transcript.sh invoking this module) is covered by common_transcript_test.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record
from imbue.mngr_claude.resources import common_transcript_convert


def _write(input_file: Path, lines: list[Any]) -> None:
    input_file.write_text("\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n")


def _events(output_file: Path) -> list[dict[str, Any]]:
    if not output_file.exists():
        return []
    return [json.loads(line) for line in output_file.read_text().splitlines() if line.strip()]


def _assistant(uuid: str, text: str = "hi", tool_uses: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    for tu in tool_uses or []:
        content.append({"type": "tool_use", "id": tu["id"], "name": tu["name"], "input": tu.get("input", {})})
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": "2026-01-01T00:00:01Z",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-8",
            "content": content,
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 80,
                "cache_creation_input_tokens": 20,
            },
        },
    }


def _user_text(uuid: str, text: str, is_meta: bool = False) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": "2026-01-01T00:00:00Z",
        "isMeta": is_meta,
        "message": {"role": "user", "content": text},
    }


def _user_tool_result(uuid: str, tool_use_id: str, output: Any, is_error: bool = False) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": "2026-01-01T00:00:02Z",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": output, "is_error": is_error}],
        },
    }


def test_converts_assistant_message_with_tokens_and_tool_calls(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file, [_assistant("u1", text="hello", tool_uses=[{"id": "t1", "name": "Bash", "input": {"cmd": "ls"}}])]
    )
    count = common_transcript_convert.convert(str(input_file), str(output_file))
    assert count == 1
    event = _events(output_file)[0]
    assert event["type"] == "assistant_message"
    assert event["model"] == "claude-opus-4-8"
    assert event["text"] == "hello"
    assert event["usage"] == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 80,
        "cache_write_tokens": 20,
    }
    assert event["tool_calls"][0]["tool_name"] == "Bash"
    assert validate_common_transcript_record(event) is None


def test_assistant_parts_preserve_text_then_tool_order(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file, [_assistant("u1", text="hello", tool_uses=[{"id": "t1", "name": "Bash", "input": {"cmd": "ls"}}])]
    )
    common_transcript_convert.convert(str(input_file), str(output_file))
    event = _events(output_file)[0]
    # parts preserve the source order (text block, then tool_use block), unlike the
    # flat text + tool_calls split.
    assert event["parts"] == [
        {"type": "text", "content": "hello"},
        {"type": "tool_call", "tool_call_id": "t1", "tool_name": "Bash", "input_preview": '{"cmd":"ls"}'},
    ]


def test_converts_user_text_message(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_user_text("u1", "hi there")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 1
    event = _events(output_file)[0]
    assert event["type"] == "user_message"
    assert event["content"] == "hi there"


def test_meta_user_message_reclassified_as_tool_result(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_user_text("u1", "stop hook output", is_meta=True)])
    event = _events(output_file)[0] if common_transcript_convert.convert(str(input_file), str(output_file)) else {}
    assert event["type"] == "tool_result"
    assert event["tool_name"] == "meta"


def test_tool_result_labeled_from_preceding_tool_use(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [
            _assistant("u1", text="", tool_uses=[{"id": "t1", "name": "Read"}]),
            _user_tool_result("u2", "t1", [{"type": "text", "text": "file contents"}]),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file))
    results = [e for e in _events(output_file) if e["type"] == "tool_result"]
    assert results[0]["tool_name"] == "Read"
    assert results[0]["output"] == "file contents"


def test_dedup_against_existing_output(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_assistant("u1")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 1
    # Re-running over the same input must not re-append (ID-based dedup).
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 0
    assert len(_events(output_file)) == 1


def test_malformed_input_line_is_skipped(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, ["{not json", _user_text("u1", "real message")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 1


def test_corrupt_existing_output_line_is_skipped(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    output_file.write_text("{corrupt existing line\n")
    _write(input_file, [_user_text("u1", "real message")])
    # A corrupt pre-existing output line must not abort the run.
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 1


def test_non_utf8_byte_in_input_does_not_abort(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    # Raw transcript streams can carry arbitrary bytes (e.g. tool output). A single
    # undecodable byte must not abort the (append-only) conversion pass.
    valid_line = json.dumps(_user_text("u1", "real message")).encode()
    input_file.write_bytes(b"\xff\xfe garbage byte line\n" + valid_line + b"\n")
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 1
    assert [e.get("content") for e in _events(output_file)] == ["real message"]


def test_null_message_line_is_dropped(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    # A null (rather than dict) message carries no usable content, so the line is
    # dropped -- not raised on (AttributeError aborting the run) and not emitted as
    # an empty event. A following valid line must still convert.
    _write(
        input_file,
        [
            {"type": "assistant", "uuid": "u1", "timestamp": "2026-01-01T00:00:01Z", "message": None},
            {"type": "user", "uuid": "u2", "timestamp": "2026-01-01T00:00:02Z", "message": None},
            _user_text("u3", "real message"),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file))
    # Only the valid line is emitted; the two null-message lines produce nothing.
    assert [e.get("content") for e in _events(output_file)] == ["real message"]


def test_events_without_uuid_or_timestamp_are_skipped(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [{"type": "user", "message": {"content": "no uuid"}}])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 0


def test_missing_input_file_returns_zero(tmp_path: Path) -> None:
    assert common_transcript_convert.convert(str(tmp_path / "missing.jsonl"), str(tmp_path / "out.jsonl")) == 0


def test_long_tool_output_is_truncated(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    long_output = "x" * (common_transcript_convert._MAX_OUTPUT_LENGTH + 100)
    _write(
        input_file,
        [
            _assistant("u1", text="", tool_uses=[{"id": "t1", "name": "Bash"}]),
            _user_tool_result("u2", "t1", long_output),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file))
    result = [e for e in _events(output_file) if e["type"] == "tool_result"][0]
    assert result["output"].endswith("...")
    assert len(result["output"]) == common_transcript_convert._MAX_OUTPUT_LENGTH + 3
