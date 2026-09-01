"""Unit tests for the codex common-transcript converter (common_transcript_convert.py).

Exercises ``convert`` and its helpers directly against codex rollout streams on
disk -- both synthetic shapes and a real rollout captured from the patched codex
0.146.0 build (test_fixtures/codex_0146_rollout_exec_turn.jsonl) -- without the
surrounding shell script. The shell integration (common_transcript.sh invoking
this module) is covered by common_transcript_test.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from imbue.mngr.agents.common_transcript_records import PINNED_ATIF_SCHEMA_VERSION
from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record
from imbue.mngr_codex.resources import common_transcript_convert
from imbue.mngr_codex.resources.testing import DEFAULT_ROLLOUT_TIMESTAMP
from imbue.mngr_codex.resources.testing import rollout_assistant_message as _assistant
from imbue.mngr_codex.resources.testing import rollout_function_call as _function_call
from imbue.mngr_codex.resources.testing import rollout_function_call_output as _function_call_output
from imbue.mngr_codex.resources.testing import rollout_line as _line
from imbue.mngr_codex.resources.testing import rollout_reasoning as _reasoning
from imbue.mngr_codex.resources.testing import rollout_user_message as _user

# A verbatim rollout captured live from the patched codex 0.146.0 build: one turn
# that ran `echo fixture-marker && cat /etc/hostname` through the unified exec
# tool (custom_tool_call / custom_tool_call_output) with the AGENTS.md context
# injection riding in as a user-role message.
_REAL_0146_ROLLOUT = Path(__file__).parent / "test_fixtures" / "codex_0146_rollout_exec_turn.jsonl"


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
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_user("hello")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 2
    assert _records(output_file)[0] == {
        "type": "header",
        "event_id": common_transcript_convert._header_event_id(""),
        "emitter": "codex/common_transcript",
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
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_user("first")])
    common_transcript_convert.convert(str(input_file), str(output_file))
    _write(input_file, [_user("first"), _assistant("second")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 1

    records = _records(output_file)
    assert [r["type"] for r in records] == ["header", "step", "step"]


def test_converts_user_and_assistant_messages(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_user("hello"), _assistant("hi back")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 3
    steps = _steps(output_file)
    assert steps[0] == {
        "type": "step",
        "event_id": common_transcript_convert._make_event_id(DEFAULT_ROLLOUT_TIMESTAMP, "hello", "user"),
        "emitter": "codex/common_transcript",
        "timestamp": "2026-06-09T07:00:00.000Z",
        "source": "user",
        "message": "hello",
    }
    assert steps[1]["source"] == "agent"
    assert steps[1]["message"] == "hi back"
    # No usage or model is available in the rollout, so those ATIF fields stay absent.
    assert "metrics" not in steps[1]
    assert "model_name" not in steps[1]
    _assert_all_conform(_records(output_file))


def test_legacy_line_index_ids_still_dedupe_reprocessing(tmp_path: Path) -> None:
    """Output written before the content-hash ids carries line-index ids; a re-run
    must recognize them rather than re-appending the lines it already converted."""
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_user("hello")])
    output_file.write_text(json.dumps({"event_id": "line-1-user"}) + "\n")

    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 0
    assert _records(output_file) == [{"event_id": "line-1-user"}]


def test_function_call_and_output_pair_by_native_call_id(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [_function_call("shell", '{"cmd":"ls"}', "call-1"), _function_call_output("call-1", "file-a\nfile-b")],
    )
    common_transcript_convert.convert(str(input_file), str(output_file))

    call_step = _steps(output_file)[0]
    assert call_step["source"] == "agent"
    assert call_step["message"] == ""
    # The tool_call_id is codex's own call_id, so the doc-builder pairs the result
    # back to this step without any synthetic id.
    assert call_step["tool_calls"] == [
        {"tool_call_id": "call-1", "function_name": "shell", "arguments": {"cmd": "ls"}}
    ]

    observation = _observations(output_file)[0]
    assert observation["results"] == [
        {
            "source_call_id": "call-1",
            "content": "file-a\nfile-b",
            "extra": {"is_error": False, "tool_name": "shell"},
        }
    ]
    _assert_all_conform(_records(output_file))


def test_call_and_output_converted_in_separate_passes_still_pair(tmp_path: Path) -> None:
    # Live conversion is incremental: the call is usually converted one pass before
    # its output arrives. On that second pass the call's line is re-read and skipped
    # as a duplicate, so the tool name must be remembered BEFORE the dedup skip --
    # otherwise every real result would land under the "unknown" tool name.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    call = _function_call("shell", '{"cmd":"ls"}', "call-1")
    _write(input_file, [call])
    common_transcript_convert.convert(str(input_file), str(output_file))
    assert _observations(output_file) == []

    _write(input_file, [call, _function_call_output("call-1", "file-a")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 1

    assert _observations(output_file)[0]["results"][0] == {
        "source_call_id": "call-1",
        "content": "file-a",
        "extra": {"is_error": False, "tool_name": "shell"},
    }
    _assert_all_conform(_records(output_file))


def test_custom_tool_call_invocation_is_preserved_whole(tmp_path: Path) -> None:
    # The 0.146 unified exec tool emits custom_tool_call (invocation under "input",
    # which is JavaScript, not JSON) / custom_tool_call_output; the invocation is
    # not a JSON object, so it rides whole under _raw.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    invocation = 'tools.exec_command({"cmd":"ls"}); text(r.output);'
    call = _line("response_item", {"type": "custom_tool_call", "name": "exec", "input": invocation, "call_id": "c1"})
    output = _line(
        "response_item",
        {"type": "custom_tool_call_output", "call_id": "c1", "output": [{"type": "input_text", "text": "file-a\n"}]},
    )
    _write(input_file, [call, output])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 3

    assert _steps(output_file)[0]["tool_calls"] == [
        {"tool_call_id": "c1", "function_name": "exec", "arguments": {"_raw": invocation}}
    ]
    assert _observations(output_file)[0]["results"][0] == {
        "source_call_id": "c1",
        "content": "file-a\n",
        "extra": {"is_error": False, "tool_name": "exec"},
    }
    _assert_all_conform(_records(output_file))


def test_large_tool_input_and_output_survive_untruncated(tmp_path: Path) -> None:
    # The ATIF stream is full fidelity: no preview caps on arguments, no output cap.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    long_command = "echo " + "a" * 500
    long_output = "x" * 5000
    _write(
        input_file,
        [
            _function_call("shell", json.dumps({"cmd": long_command}), "call-1"),
            _function_call_output("call-1", long_output),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file))

    assert _steps(output_file)[0]["tool_calls"][0]["arguments"] == {"cmd": long_command}
    assert _observations(output_file)[0]["results"][0]["content"] == long_output
    _assert_all_conform(_records(output_file))


def test_reasoning_item_becomes_an_agent_step_with_reasoning_content(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_reasoning("first thought", "second thought"), _assistant("done")])
    common_transcript_convert.convert(str(input_file), str(output_file))

    reasoning_step = _steps(output_file)[0]
    assert reasoning_step["event_id"] == common_transcript_convert._make_event_id(
        DEFAULT_ROLLOUT_TIMESTAMP, reasoning_step["reasoning_content"], "reasoning"
    )
    assert reasoning_step["source"] == "agent"
    assert reasoning_step["message"] == ""
    # Multiple thinking blocks in one inference are joined with a blank line.
    assert reasoning_step["reasoning_content"] == "first thought\n\nsecond thought"
    _assert_all_conform(_records(output_file))


def test_reasoning_item_with_only_encrypted_content_is_dropped(tmp_path: Path) -> None:
    # The captured 0.146 rollout's reasoning items are exactly this shape.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_reasoning(), _user("real")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 2
    assert [s["source"] for s in _steps(output_file)] == ["user"]


def test_reasoning_text_is_also_read_from_the_content_array(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    item = _line(
        "response_item",
        {
            "type": "reasoning",
            "summary": "not-a-list",
            "content": ["bare", {"type": "reasoning_text", "text": "deliberating"}],
        },
    )
    _write(input_file, [item])
    common_transcript_convert.convert(str(input_file), str(output_file))
    assert _steps(output_file)[0]["reasoning_content"] == "deliberating"


def test_instruction_injections_become_system_steps(tmp_path: Path) -> None:
    # All three injection envelopes are session-configured instructions, carried in
    # full on system steps; the genuine turn still converts as a user step.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    agents_md = "# AGENTS.md instructions for /some/dir\n\n<INSTRUCTIONS>\nbe good\n</INSTRUCTIONS>"
    _write(
        input_file,
        [
            _user(agents_md),
            _user("<user_instructions>\nalways answer in haiku\n</user_instructions>"),
            _user("<environment_context>\ncwd: /tmp\n</environment_context>"),
            _user("genuine question"),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file))

    steps = _steps(output_file)
    assert [s["source"] for s in steps] == ["system", "system", "system", "user"]
    # The injection/user split rides in the id's kind suffix as well as in `source`.
    assert [s["event_id"] for s in steps] == [
        common_transcript_convert._make_event_id(DEFAULT_ROLLOUT_TIMESTAMP, s["message"], "system") for s in steps[:3]
    ] + [common_transcript_convert._make_event_id(DEFAULT_ROLLOUT_TIMESTAMP, "genuine question", "user")]
    # The full instruction text survives -- these are the session's configuration.
    assert steps[0]["message"] == agents_md
    assert steps[3]["message"] == "genuine question"
    _assert_all_conform(_records(output_file))


def test_agents_md_lookalike_without_envelope_is_a_user_step(tmp_path: Path) -> None:
    # A genuine user message that merely starts like the AGENTS.md header (no
    # <INSTRUCTIONS> envelope) must NOT be reclassified as a system step.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_user("# AGENTS.md instructions for this repo look wrong, can you fix them?")])
    common_transcript_convert.convert(str(input_file), str(output_file))
    assert _steps(output_file)[0]["source"] == "user"


def test_real_0146_rollout_surfaces_paired_tool_activity(tmp_path: Path) -> None:
    # Fixture-driven guard against silent drops: the real 0.146 rollout ran one
    # command, so the converted transcript must carry a NONZERO amount of tool
    # activity (a schema-valid but tool-free output is exactly the original bug).
    output_file = tmp_path / "out.jsonl"
    assert common_transcript_convert.convert(str(_REAL_0146_ROLLOUT), str(output_file)) > 0
    records = _records(output_file)
    tool_calls = [call for r in _steps(output_file) for call in r.get("tool_calls", [])]
    results = [result for r in _observations(output_file) for result in r["results"]]
    assert len(tool_calls) == 1, "real rollout yielded the wrong number of tool calls (silent drop)"
    assert len(results) == 1, "real rollout yielded the wrong number of tool results (silent drop)"
    # Every result pairs back to an emitted tool_call by codex's native call id.
    call_ids = {call["tool_call_id"] for call in tool_calls}
    for result in results:
        assert result["source_call_id"] in call_ids
    assert tool_calls[0]["function_name"] == "exec"
    assert "echo fixture-marker" in tool_calls[0]["arguments"]["_raw"]
    assert "fixture-marker" in results[0]["content"]
    _assert_all_conform(records)


def test_real_0146_rollout_separates_the_user_turn_from_the_injection(tmp_path: Path) -> None:
    # The AGENTS.md injection arrives as a giant user-role message: it becomes a
    # system step, leaving exactly one genuine user turn.
    output_file = tmp_path / "out.jsonl"
    common_transcript_convert.convert(str(_REAL_0146_ROLLOUT), str(output_file))
    steps = _steps(output_file)
    assert [s["message"] for s in steps if s["source"] == "user"] == ["run: echo fixture-marker && cat /etc/hostname"]
    system_messages = [s["message"] for s in steps if s["source"] == "system"]
    assert len(system_messages) == 1
    assert system_messages[0].startswith("# AGENTS.md instructions for ")
    assert "IT IS CRITICAL TO FOLLOW ALL INSTRUCTIONS" in system_messages[0]


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
    assert _observations(output_file)[0]["results"][0]["content"] == "part-apart-b"


def test_unpaired_output_is_emitted_with_an_unknown_tool_name(tmp_path: Path) -> None:
    # A rollout tailed from mid-turn can carry an output whose call was never seen.
    # The result must still reach the stream (the doc-builder warns on the
    # unmatched id); dropping it would lose the tool's output entirely.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_function_call_output("orphan", "output nobody asked for")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 2
    assert _observations(output_file)[0]["results"][0] == {
        "source_call_id": "orphan",
        "content": "output nobody asked for",
        "extra": {"is_error": False, "tool_name": "unknown"},
    }
    _assert_all_conform(_records(output_file))


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
    common_transcript_convert.convert(str(input_file), str(output_file))
    assert [s["source"] for s in _steps(output_file)] == ["user"]


def test_empty_user_message_is_dropped(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_user("")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 0


def test_content_free_assistant_message_is_dropped(tmp_path: Path) -> None:
    # An assistant message with no output_text carries no signal: codex models a
    # tool invocation as its own rollout item, so there is nothing else on it.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_assistant(""), _user("real")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 2
    assert [s["source"] for s in _steps(output_file)] == ["user"]


def test_dedup_against_existing_output(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_user("hello")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 2
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 0
    assert len(_records(output_file)) == 2


def test_malformed_line_is_skipped(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, ["{ not valid json", _user("after the broken line")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 2
    assert _steps(output_file)[0]["message"] == "after the broken line"


def test_corrupt_existing_output_line_is_skipped(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    output_file.write_text("{corrupt existing line\n")
    _write(input_file, [_user("real")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 2


def test_missing_input_file_returns_zero(tmp_path: Path) -> None:
    output_file = tmp_path / "out.jsonl"
    assert common_transcript_convert.convert(str(tmp_path / "missing.jsonl"), str(output_file)) == 0
    # Not even a header: an agent with no rollout yet has no stream.
    assert not output_file.exists()


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


def test_parse_arguments_covers_every_native_shape() -> None:
    parse = common_transcript_convert._parse_arguments
    assert parse('{"cmd":"ls"}') == {"cmd": "ls"}
    # An absent/empty payload means "no arguments", not a raw empty string.
    assert parse("") == {}
    # A string that parses to a non-object, and one that does not parse at all,
    # are both preserved whole rather than dropped.
    assert parse("[1, 2]") == {"_raw": "[1, 2]"}
    assert parse("not json at all") == {"_raw": "not json at all"}
    # An already-decoded object is used verbatim; any other JSON value is dumped.
    assert parse({"cmd": "ls"}) == {"cmd": "ls"}
    assert parse([1, 2]) == {"_raw": "[1,2]"}


def test_blank_non_dict_and_non_dict_payload_input_lines_are_skipped(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    # A blank line, a JSON array (non-dict), and a response_item whose payload is
    # not a dict are all skipped; the real message still converts.
    _write(
        input_file,
        [
            "",
            "[1, 2, 3]",
            {"timestamp": "2026-06-09T07:00:00.000Z", "type": "response_item", "payload": "not-a-dict"},
            _user("real"),
        ],
    )
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 2
    assert _steps(output_file)[0]["message"] == "real"


def test_missing_timestamp_falls_back_to_conversion_time(tmp_path: Path) -> None:
    # ATIF requires a timestamp on every step; a rollout line that lost its own
    # still has to produce a valid record.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    timestampless = _user("hi")
    del timestampless["timestamp"]
    _write(input_file, [timestampless])
    common_transcript_convert.convert(str(input_file), str(output_file))
    assert _steps(output_file)[0]["timestamp"].endswith("Z")
    _assert_all_conform(_records(output_file))


def test_unknown_response_item_payload_type_is_ignored(tmp_path: Path) -> None:
    # A response_item with an unrecognized payload.type is bookkeeping, not content.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_line("response_item", {"type": "web_search_call", "status": "completed"}), _user("real")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 2


def test_developer_role_messages_are_ignored(tmp_path: Path) -> None:
    # codex's own developer-role items are harness plumbing, not conversation turns.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    developer = _line(
        "response_item",
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "# Codex instructions"}]},
    )
    _write(input_file, [developer, _user("real")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 2


def test_call_and_output_without_call_id_are_skipped(tmp_path: Path) -> None:
    # An empty call_id can't carry a tool_call_id / source_call_id, so neither the
    # call nor its output can be recorded.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_function_call("shell", "{}", ""), _function_call_output("", "out")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 0


def test_non_string_function_call_arguments_are_used_verbatim(tmp_path: Path) -> None:
    # arguments emitted as an object (not a string) is already the ATIF arguments object.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    call = _line(
        "response_item", {"type": "function_call", "name": "shell", "arguments": {"cmd": "ls"}, "call_id": "c1"}
    )
    _write(input_file, [call, _function_call_output("c1", "done")])
    common_transcript_convert.convert(str(input_file), str(output_file))
    assert _steps(output_file)[0]["tool_calls"][0]["arguments"] == {"cmd": "ls"}


def test_non_string_tool_name_becomes_empty(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    call = _line("response_item", {"type": "function_call", "name": 7, "arguments": "{}", "call_id": "c1"})
    _write(input_file, [call])
    common_transcript_convert.convert(str(input_file), str(output_file))
    assert _steps(output_file)[0]["tool_calls"][0]["function_name"] == ""


def test_dedup_skips_existing_steps_and_observations(tmp_path: Path) -> None:
    # Re-running convert must not re-append the agent steps or the observation
    # already present in the output.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_assistant("hi"), _function_call("shell", "{}", "c1"), _function_call_output("c1", "ok")])
    first = common_transcript_convert.convert(str(input_file), str(output_file))
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 0
    assert len(_records(output_file)) == first


def test_blank_line_in_existing_output_is_skipped(tmp_path: Path) -> None:
    # A blank line in the existing output file is ignored while loading event ids.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    output_file.write_text("\n")
    _write(input_file, [_user("real")])
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 2


def test_non_utf8_byte_in_input_does_not_abort(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    # Raw rollout streams can carry arbitrary bytes; a single undecodable byte must
    # not abort the (append-only) conversion pass.
    valid_line = json.dumps(_user("real")).encode()
    input_file.write_bytes(b"\xff\xfe garbage byte line\n" + valid_line + b"\n")
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 2
    assert _steps(output_file)[0]["source"] == "user"
