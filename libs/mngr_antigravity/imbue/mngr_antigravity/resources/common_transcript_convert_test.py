"""Unit tests for the antigravity common-transcript converter (common_transcript_convert.py).

Exercises ``convert`` and its helpers directly against synthetic raw-transcript
streams on disk, without the surrounding shell script. The shell integration
(common_transcript.sh invoking this module) is covered by common_transcript_test.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from imbue.mngr.agents.common_transcript_records import PINNED_ATIF_SCHEMA_VERSION
from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record
from imbue.mngr_antigravity.resources import common_transcript_convert
from imbue.mngr_antigravity.resources.testing import code_action_event as _code_action
from imbue.mngr_antigravity.resources.testing import conversation_history_event as _conversation_history
from imbue.mngr_antigravity.resources.testing import planner_response_event as _planner_response
from imbue.mngr_antigravity.resources.testing import user_input_event as _user_input


def _write(input_file: Path, lines: list[Any]) -> None:
    input_file.write_text("\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n")


def _records(output_file: Path) -> list[dict[str, Any]]:
    if not output_file.exists():
        return []
    return [json.loads(line) for line in output_file.read_text().splitlines() if line.strip()]


def _steps(output_file: Path) -> list[dict[str, Any]]:
    return [r for r in _records(output_file) if r["type"] == "step"]


def _observations(output_file: Path) -> list[dict[str, Any]]:
    return [r for r in _records(output_file) if r["type"] == "observation"]


def _assert_all_conform(records: list[dict[str, Any]]) -> None:
    for record in records:
        assert validate_common_transcript_record(record) is None, record


def test_stream_opens_with_a_header_record(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(in_f, [_user_input("c1", 0, "hi")])
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 2
    assert _records(out_f)[0] == {
        "type": "header",
        "event_id": common_transcript_convert._header_event_id(""),
        "emitter": "antigravity/common_transcript",
        "schema_version": "ATIF-v1.7",
    }


def test_header_ids_differ_between_agents() -> None:
    # Analytics dedupes the fleet by event id, so two agents' headers must not collide.
    assert common_transcript_convert._header_event_id("agent-a") != common_transcript_convert._header_event_id(
        "agent-b"
    )


def test_pinned_schema_version_matches_the_canonical_one() -> None:
    # The converter is stdlib-only and cannot import the canonical schema, so the
    # revision it stamps on the header is a hand-copied constant; this pins it.
    assert common_transcript_convert._SCHEMA_VERSION == PINNED_ATIF_SCHEMA_VERSION


def test_header_is_written_exactly_once_across_two_passes(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    first = _user_input("c1", 0, "hi")
    _write(in_f, [first])
    common_transcript_convert.convert(str(in_f), str(out_f))
    _write(in_f, [first, _planner_response("c1", 1, text="ok")])
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 1

    assert [r["type"] for r in _records(out_f)] == ["header", "step", "step"]


def test_user_input_becomes_a_user_step(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(in_f, [_user_input("c1", 0, "  hi  ")])
    common_transcript_convert.convert(str(in_f), str(out_f))
    assert _steps(out_f)[0] == {
        "type": "step",
        "event_id": "c1-0-user",
        "emitter": "antigravity/common_transcript",
        "timestamp": "2026-05-21T07:00:00Z",
        "source": "user",
        "message": "hi",
        # agy's own annotations are ATIF step extras, not top-level record fields.
        "extra": {"conversation_id": "c1", "step_index": 0},
    }
    _assert_all_conform(_records(out_f))


def test_non_string_user_content_is_dropped(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(in_f, [_user_input("c1", 0, {"x": 1})])
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 0


def test_planner_response_with_tool_call_and_code_action_pair(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        in_f,
        [
            _planner_response(
                "c1",
                1,
                text="running a tool",
                tool_calls=[{"name": "run_command", "args": '{"cmd":"ls"}'}],
            ),
            _code_action("c1", 2, "output text"),
        ],
    )
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 3

    step = _steps(out_f)[0]
    assert step["source"] == "agent"
    assert step["message"] == "running a tool"
    # agy's args arrive as a JSON string; the full decoded object is recorded.
    assert step["tool_calls"] == [
        {"tool_call_id": "c1-1-tc0", "function_name": "run_command", "arguments": {"cmd": "ls"}}
    ]
    # The CODE_ACTION pairs with the preceding tool call's synthetic id.
    assert _observations(out_f)[0]["results"] == [
        {
            "source_call_id": "c1-1-tc0",
            "content": "output text",
            "extra": {
                "is_error": False,
                "tool_name": "run_command",
                "conversation_id": "c1",
                "step_index": 2,
            },
        }
    ]
    _assert_all_conform(_records(out_f))


def test_call_and_code_action_converted_in_separate_passes_still_pair(tmp_path: Path) -> None:
    # Live conversion is incremental: the PLANNER_RESPONSE is usually converted one
    # pass before its CODE_ACTION arrives. On that second pass the planner line is
    # re-read and its step skipped as a duplicate, so the pending call must be
    # recorded BEFORE the dedup skip -- otherwise the result would be dropped for
    # having nothing to attach to.
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    planner = _planner_response("c1", 1, tool_calls=[{"name": "run_command", "args": "{}"}])
    _write(in_f, [planner])
    common_transcript_convert.convert(str(in_f), str(out_f))
    assert _observations(out_f) == []

    _write(in_f, [planner, _code_action("c1", 2, "output text")])
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 1

    result = _observations(out_f)[0]["results"][0]
    assert result["source_call_id"] == "c1-1-tc0"
    assert result["extra"]["tool_name"] == "run_command"
    _assert_all_conform(_records(out_f))


def test_degraded_timestamp_does_not_reorder_a_step_before_its_observation(tmp_path: Path) -> None:
    # The decoder degrades a corrupt created_at to "", and the converter then stamps
    # conversion time (always later than any recorded event). Emission must follow
    # input order regardless: the doc-builder reads append order as authoritative, so
    # an observation appended ahead of its own call's step would lose its pairing.
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        in_f,
        [
            _planner_response("c1", 1, tool_calls=[{"name": "run_command", "args": "{}"}], timestamp=""),
            _code_action("c1", 2, "output text"),
        ],
    )
    common_transcript_convert.convert(str(in_f), str(out_f))

    assert [r["type"] for r in _records(out_f)] == ["header", "step", "observation"]
    assert _observations(out_f)[0]["results"][0]["source_call_id"] == "c1-1-tc0"
    _assert_all_conform(_records(out_f))


def test_planner_thinking_becomes_reasoning_content(tmp_path: Path) -> None:
    # decode_agy_transcript.py hangs CortexStepPlannerResponse's thinking on the same
    # PLANNER_RESPONSE record, so it lands on that step with nothing to merge.
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        in_f,
        [
            _planner_response(
                "c1",
                1,
                text="here goes",
                thinking="the user wants the file listed",
                tool_calls=[{"name": "run_command", "args": "{}"}],
            )
        ],
    )
    common_transcript_convert.convert(str(in_f), str(out_f))
    step = _steps(out_f)[0]
    assert step["reasoning_content"] == "the user wants the file listed"
    assert step["message"] == "here goes"
    _assert_all_conform(_records(out_f))


def test_planner_response_without_thinking_omits_reasoning_content(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(in_f, [_planner_response("c1", 1, text="plain")])
    common_transcript_convert.convert(str(in_f), str(out_f))
    step = _steps(out_f)[0]
    assert "reasoning_content" not in step
    assert "tool_calls" not in step


def test_thinking_only_planner_response_is_still_recorded(tmp_path: Path) -> None:
    # A planner turn that produced only thinking still carries its reasoning.
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(in_f, [_planner_response("c1", 1, thinking="just pondering")])
    common_transcript_convert.convert(str(in_f), str(out_f))
    step = _steps(out_f)[0]
    assert step["message"] == ""
    assert step["reasoning_content"] == "just pondering"
    _assert_all_conform(_records(out_f))


def test_large_tool_input_and_output_survive_untruncated(tmp_path: Path) -> None:
    # The ATIF stream is full fidelity: no preview caps on arguments, no output cap.
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    long_command = "echo " + "a" * 500
    long_output = "x" * 5000
    _write(
        in_f,
        [
            _planner_response(
                "c1", 1, tool_calls=[{"name": "run_command", "args": json.dumps({"cmd": long_command})}]
            ),
            _code_action("c1", 2, long_output),
        ],
    )
    common_transcript_convert.convert(str(in_f), str(out_f))
    assert _steps(out_f)[0]["tool_calls"][0]["arguments"] == {"cmd": long_command}
    assert _observations(out_f)[0]["results"][0]["content"] == long_output
    _assert_all_conform(_records(out_f))


def test_code_action_error_status_sets_is_error(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        in_f,
        [
            _planner_response("c1", 1, tool_calls=[{"name": "run_command", "args": "{}"}]),
            _code_action("c1", 2, "boom", status="ERROR"),
        ],
    )
    common_transcript_convert.convert(str(in_f), str(out_f))
    assert _observations(out_f)[0]["results"][0]["extra"]["is_error"] is True


def test_code_action_with_non_string_content_is_dropped(tmp_path: Path) -> None:
    # A CODE_ACTION whose content is JSON null (key present, value null) carries no
    # usable output text, so it is dropped rather than emitted as an empty result.
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    code_action_with_null_content = _code_action("c1", 2, "placeholder")
    code_action_with_null_content["content"] = None
    _write(
        in_f,
        [
            _planner_response("c1", 1, tool_calls=[{"name": "run_command", "args": "{}"}]),
            code_action_with_null_content,
        ],
    )
    common_transcript_convert.convert(str(in_f), str(out_f))
    assert _observations(out_f) == []


def test_code_action_without_preceding_tool_call_is_dropped(tmp_path: Path) -> None:
    # ATIF requires source_call_id, and agy's CODE_ACTION carries no id of its own.
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(in_f, [_code_action("c1", 2, "orphan")])
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 0


def test_unknown_source_type_is_dropped(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(in_f, [_conversation_history("c1", 0)])
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
    _write(in_f, [_user_input("c1", 0, "hi")])
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 2
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 0
    assert len(_records(out_f)) == 2


def test_malformed_line_is_skipped(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(in_f, ["{ not valid json", _user_input("c1", 0, "ok")])
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 2


def test_corrupt_existing_output_line_is_skipped(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    out_f.write_text("{corrupt existing line\n")
    _write(in_f, [_user_input("c1", 0, "hi")])
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 2


def test_missing_input_file_returns_zero(tmp_path: Path) -> None:
    out_f = tmp_path / "out.jsonl"
    assert common_transcript_convert.convert(str(tmp_path / "missing.jsonl"), str(out_f)) == 0
    # Not even a header: an agent with no transcript yet has no stream.
    assert not out_f.exists()


def test_parse_arguments_covers_every_native_shape() -> None:
    parse = common_transcript_convert._parse_arguments
    assert parse('{"cmd":"ls"}') == {"cmd": "ls"}
    # An absent/empty native payload means "no arguments", not a raw empty string.
    assert parse("") == {}
    # A string that parses to a non-object, and one that does not parse at all,
    # are both preserved whole rather than dropped.
    assert parse("[1, 2]") == {"_raw": "[1, 2]"}
    assert parse("not json at all") == {"_raw": "not json at all"}
    # An already-decoded object is used verbatim; any other JSON value is dumped.
    assert parse({"cmd": "ls"}) == {"cmd": "ls"}
    assert parse([1, 2]) == {"_raw": "[1,2]"}


def test_non_dict_tool_call_and_non_string_name_are_handled(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(in_f, [_planner_response("c1", 1, tool_calls=["not-a-dict", {"name": 7, "args": "{}"}])])
    common_transcript_convert.convert(str(in_f), str(out_f))
    assert _steps(out_f)[0]["tool_calls"] == [{"tool_call_id": "c1-1-tc1", "function_name": "", "arguments": {}}]


def test_non_string_planner_content_becomes_an_empty_message(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(in_f, [_planner_response("c1", 1, text={"x": 1})])
    common_transcript_convert.convert(str(in_f), str(out_f))
    assert _steps(out_f)[0]["message"] == ""


def test_missing_timestamp_falls_back_to_conversion_time(tmp_path: Path) -> None:
    # The decoder degrades a corrupt created_at to ""; ATIF still requires a timestamp.
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(in_f, [_user_input("c1", 0, "hi", timestamp="")])
    common_transcript_convert.convert(str(in_f), str(out_f))
    assert _steps(out_f)[0]["timestamp"].endswith("Z")
    _assert_all_conform(_records(out_f))


def test_non_utf8_byte_in_input_does_not_abort(tmp_path: Path) -> None:
    in_f, out_f = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    # Raw transcript streams can carry arbitrary bytes; a single undecodable byte
    # must not abort the (append-only) conversion pass.
    valid_line = json.dumps(_user_input("c1", 0, "real")).encode()
    in_f.write_bytes(b"\xff\xfe garbage byte line\n" + valid_line + b"\n")
    assert common_transcript_convert.convert(str(in_f), str(out_f)) == 2
    assert _steps(out_f)[0]["message"] == "real"
