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
    # A text-only codex turn carries the text as a single (trivially ordered) part.
    assert events[1]["parts"] == [{"type": "text", "content": "hi back"}]
    assert events[1]["parts_ordered"] is True
    for event in events:
        assert validate_common_transcript_record(event) is None


def test_function_call_and_output_pair_into_tool_result(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [_function_call("shell", '{"cmd":"ls"}', "call-1"), _function_call_output("call-1", "file-a\nfile-b")],
    )
    common_transcript_convert.convert(str(input_file), str(output_file))
    events = _events(output_file)
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["tool_name"] == "shell"
    assert results[0]["output"] == "file-a\nfile-b"
    # The call surfaces on the assistant turn as an ordered tool_call part (the
    # authoritative view the reader renders), paired to the result by tool_call_id.
    assistant = [e for e in events if e["type"] == "assistant_message"]
    assert len(assistant) == 1
    assert assistant[0]["parts"] == [
        {"type": "tool_call", "tool_call_id": "line-1-tc", "tool_name": "shell", "input_preview": '{"cmd":"ls"}'}
    ]
    assert assistant[0]["parts_ordered"] is True
    assert assistant[0]["tool_calls"][0]["tool_call_id"] == results[0]["tool_call_id"]
    assert "stop_reason" not in assistant[0]
    for event in events:
        assert validate_common_transcript_record(event) is None


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


def test_join_content_text_handles_non_list_and_non_matching_items() -> None:
    # Non-list content yields the empty string.
    assert common_transcript_convert._join_content_text("not a list", "input_text") == ""
    # Bare-string items and type-mismatched items are skipped; only the matching
    # item's text is joined.
    content = ["bare", {"type": "other", "text": "skip"}, {"type": "input_text", "text": "keep"}]
    assert common_transcript_convert._join_content_text(content, "input_text") == "keep"


def test_stringify_output_json_dumps_non_text_items_and_scalars() -> None:
    # A content-array item without a string .text is JSON-dumped.
    assert common_transcript_convert._stringify_output([{"image": "x"}]) == '{"image":"x"}'
    # A bare (non-str, non-list) value is JSON-dumped whole.
    assert common_transcript_convert._stringify_output({"k": 1}) == '{"k":1}'


def test_blank_non_dict_and_non_dict_payload_input_lines_are_skipped(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    # A blank line, a JSON array (non-dict), and a response_item whose payload is
    # not a dict are all skipped; the real message still converts.
    _write(
        input_file,
        [
            "",
            "[1, 2, 3]",
            {"timestamp": "t", "type": "response_item", "payload": "not-a-dict"},
            _user("real"),
        ],
    )
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 1
    assert _events(output_file)[0]["content"] == "real"


def test_unknown_response_item_payload_type_is_ignored(tmp_path: Path) -> None:
    # A response_item with an unrecognized payload.type is bookkeeping, not content.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_line("response_item", {"type": "reasoning", "summary": "..."}), _user("real")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 1


def test_function_call_without_call_id_is_skipped(tmp_path: Path) -> None:
    # An empty call_id can't be paired, so the call (and its later output) is dropped.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_function_call("shell", "{}", ""), _function_call_output("", "out")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 0


def test_non_string_function_call_arguments_are_json_dumped(tmp_path: Path) -> None:
    # arguments emitted as an object (not a string) is JSON-dumped for the preview.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    call = _line(
        "response_item", {"type": "function_call", "name": "shell", "arguments": {"cmd": "ls"}, "call_id": "c1"}
    )
    _write(input_file, [call, _function_call_output("c1", "done")])
    common_transcript_convert.convert(str(input_file), str(output_file))
    result = [e for e in _events(output_file) if e["type"] == "tool_result"][0]
    assert result["tool_name"] == "shell"


def test_dedup_skips_existing_assistant_and_tool_result(tmp_path: Path) -> None:
    # Re-running convert must not re-append the assistant message or the tool_result
    # already present in the output.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_assistant("hi"), _function_call("shell", "{}", "c1"), _function_call_output("c1", "ok")])
    first = common_transcript_convert.convert(str(input_file), str(output_file))
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 0
    assert len(_events(output_file)) == first


def test_blank_line_in_existing_output_is_skipped(tmp_path: Path) -> None:
    # A blank line in the existing output file is ignored while loading event ids.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    output_file.write_text("\n")
    _write(input_file, [_user("real")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 1


def test_non_utf8_byte_in_input_does_not_abort(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    # Raw rollout streams can carry arbitrary bytes; a single undecodable byte must
    # not abort the (append-only) conversion pass.
    valid_line = json.dumps(_user("real")).encode()
    input_file.write_bytes(b"\xff\xfe garbage byte line\n" + valid_line + b"\n")
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 1
    assert _events(output_file)[0]["type"] == "user_message"
