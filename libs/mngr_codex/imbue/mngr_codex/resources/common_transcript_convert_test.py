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
from imbue.mngr_codex.resources.testing import rollout_command_execution as _command_execution
from imbue.mngr_codex.resources.testing import rollout_custom_tool_call as _custom_tool_call
from imbue.mngr_codex.resources.testing import rollout_custom_tool_call_output as _custom_tool_call_output
from imbue.mngr_codex.resources.testing import rollout_function_call as _function_call
from imbue.mngr_codex.resources.testing import rollout_function_call_output as _function_call_output
from imbue.mngr_codex.resources.testing import rollout_line as _line
from imbue.mngr_codex.resources.testing import rollout_reasoning as _reasoning
from imbue.mngr_codex.resources.testing import rollout_timestamp_ms as _timestamp_ms
from imbue.mngr_codex.resources.testing import rollout_token_count as _token_count
from imbue.mngr_codex.resources.testing import rollout_turn_context as _turn_context
from imbue.mngr_codex.resources.testing import rollout_user_message as _user

# A verbatim rollout captured live from the patched codex 0.146.0 build: one turn
# that ran `echo fixture-marker && cat /etc/hostname` through the unified exec
# tool (custom_tool_call / custom_tool_call_output) with the AGENTS.md context
# injection riding in as a user-role message.
_REAL_0146_ROLLOUT = Path(__file__).parent / "test_fixtures" / "codex_0146_rollout_exec_turn.jsonl"

# A rollout captured from codex 0.154.0 running with Minds' codex features (code mode only,
# unified exec off in the user config), in the paginated history mode. One code-mode program runs
# four shell commands -- one fails, one is built from a JavaScript variable -- a second prints
# ALL_TOOLS, and a third yields a `sleep` that its wait collects and the turn's end then kills.
# Paths are rewritten under /home/user.
_REAL_0154_ROLLOUT = Path(__file__).parent / "test_fixtures" / "codex_0154_rollout_code_mode_commands.jsonl"


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


def _last_total_token_usage(rollout: Path) -> dict[str, Any]:
    """codex's own running session total, from the rollout's last token_count."""
    total: dict[str, Any] | None = None
    for line in rollout.read_text().splitlines():
        if not line.strip():
            continue
        payload = json.loads(line).get("payload")
        if isinstance(payload, dict) and payload.get("type") == "token_count":
            total = payload["info"]["total_token_usage"]
    assert total is not None
    return total


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


def test_agent_steps_carry_the_turns_model_and_reasoning_effort(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [
            _turn_context("gpt-5.6-sol", effort="high"),
            _user("do it"),
            _reasoning("thinking"),
            _assistant("done"),
            _token_count(input_tokens=100, output_tokens=10),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file))

    steps = _steps(output_file)
    agent_steps = [s for s in steps if s["source"] == "agent"]
    assert len(agent_steps) == 2
    # Every agent step of the turn ran under the same model and effort.
    assert all(s["model_name"] == "gpt-5.6-sol" for s in agent_steps)
    assert all(s["reasoning_effort"] == "high" for s in agent_steps)
    # A user step is not an inference, so the agent-only fields stay off it.
    user_step = next(s for s in steps if s["source"] == "user")
    assert "model_name" not in user_step
    assert "reasoning_effort" not in user_step
    _assert_all_conform(_records(output_file))


def test_turn_context_without_effort_leaves_reasoning_effort_absent(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [_turn_context("gpt-5.6-sol", effort=None), _assistant("done"), _token_count(input_tokens=1, output_tokens=1)],
    )
    common_transcript_convert.convert(str(input_file), str(output_file))
    step = _steps(output_file)[0]
    assert step["model_name"] == "gpt-5.6-sol"
    assert "reasoning_effort" not in step


def test_token_count_measures_only_the_last_agent_step_of_its_inference(tmp_path: Path) -> None:
    # One codex inference fans out over several rollout items (reasoning, message,
    # tool call), each its own agent step, and is closed by a single token_count.
    # Stamping every step of the inference would multiply the trajectory's token
    # total, so only the last one carries the metrics.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [
            _turn_context("gpt-5.6-sol"),
            _user("list the files"),
            _reasoning("deciding"),
            _assistant("running ls"),
            _function_call("shell", '{"cmd":"ls"}', "call-1"),
            # codex persists a call's output before the token_count of the
            # inference that made the call; an observation is not an agent step,
            # so it does not steal the measurement from that call.
            _function_call_output("call-1", "file-a"),
            _token_count(input_tokens=21199, output_tokens=124),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file))

    agent_steps = [s for s in _steps(output_file) if s["source"] == "agent"]
    assert [("metrics" in s) for s in agent_steps] == [False, False, True]
    assert agent_steps[2]["tool_calls"][0]["tool_call_id"] == "call-1"
    assert agent_steps[2]["metrics"]["prompt_tokens"] == 21199
    _assert_all_conform(_records(output_file))


def test_metrics_carry_codex_buckets_across_without_double_counting(tmp_path: Path) -> None:
    # codex counts cached and cache-written input inside input_tokens, and reasoning
    # output inside output_tokens, which is already ATIF's convention -- so the
    # buckets are carried across rather than summed.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [
            _assistant("done"),
            _token_count(
                input_tokens=21466,
                output_tokens=49,
                cached_input_tokens=21196,
                cache_write_input_tokens=267,
                reasoning_output_tokens=24,
                # The running session total must not be mistaken for this response's.
                total_input_tokens=42665,
            ),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file))

    assert _steps(output_file)[0]["metrics"] == {
        "prompt_tokens": 21466,
        "completion_tokens": 49,
        "cached_tokens": 21196,
        "extra": {"cache_creation_input_tokens": 267, "reasoning_output_tokens": 24},
    }
    _assert_all_conform(_records(output_file))


def test_token_count_with_no_agent_step_in_its_window_is_dropped(tmp_path: Path) -> None:
    # codex writes a turn_context again after a mid-turn compaction, and the
    # token_count that follows measures work no emitted step represents. Falling
    # back onto the previous turn's step would attribute it to the wrong inference.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [
            _turn_context("gpt-5.6-sol"),
            _assistant("first answer"),
            _token_count(input_tokens=100, output_tokens=10),
            _turn_context("gpt-5.6-sol"),
            _token_count(input_tokens=900, output_tokens=90),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)

    steps = _steps(output_file)
    assert len(steps) == 1
    assert steps[0]["metrics"]["prompt_tokens"] == 100


def test_a_second_token_count_does_not_re_measure_a_closed_inference(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [
            _assistant("done"),
            _token_count(input_tokens=100, output_tokens=10),
            _token_count(input_tokens=900, output_tokens=90),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)
    assert _steps(output_file)[0]["metrics"]["prompt_tokens"] == 100


def test_a_repeated_token_count_mid_response_measures_nothing(tmp_path: Path) -> None:
    # codex can write the previous token_count again, running total unchanged, between a
    # commentary message and the tool call of the same response. Stamping that stale usage on the
    # message would count the earlier inference twice; the response's real token_count measures
    # its call.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [
            _turn_context("gpt-5.6-terra"),
            _user("check both files"),
            _function_call("shell", '{"cmd":"cat a"}', "call-1"),
            _function_call_output("call-1", "a"),
            _token_count(input_tokens=100, output_tokens=10),
            _assistant("now the second file"),
            _token_count(input_tokens=100, output_tokens=10),
            _function_call("shell", '{"cmd":"cat b"}', "call-2"),
            _function_call_output("call-2", "b"),
            _token_count(input_tokens=300, output_tokens=30, total_input_tokens=400),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)

    agent_steps = [s for s in _steps(output_file) if s["source"] == "agent"]
    assert [s.get("metrics", {}).get("prompt_tokens") for s in agent_steps] == [100, None, 300]
    _assert_all_conform(_records(output_file))


def test_agent_step_no_token_count_ever_closes_carries_no_metrics(tmp_path: Path) -> None:
    # An aborted or interrupted response is never measured. Its step still reaches
    # the stream (at the turn-end flush) with no metrics at all -- fabricated zeros
    # would read as a real, free inference.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [
            _assistant("measured"),
            _token_count(input_tokens=100, output_tokens=10),
            _user("go on"),
            _assistant("interrupted before its usage landed"),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)

    agent_steps = [s for s in _steps(output_file) if s["source"] == "agent"]
    assert agent_steps[0]["metrics"]["prompt_tokens"] == 100
    assert "metrics" not in agent_steps[1]
    _assert_all_conform(_records(output_file))


def test_trailing_unmeasured_step_is_held_back_until_its_token_count_lands(tmp_path: Path) -> None:
    # The output is append-only and deduped by event_id, so a step emitted before
    # its token_count arrived would stay unmeasured forever. A mid-turn pass holds
    # it (and its tool result) back instead.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    measured_turn = [
        _assistant("first answer"),
        _token_count(input_tokens=100, output_tokens=10),
    ]
    in_flight = [_function_call("shell", '{"cmd":"ls"}', "call-1"), _function_call_output("call-1", "file-a")]
    _write(input_file, measured_turn + in_flight)
    common_transcript_convert.convert(str(input_file), str(output_file))
    assert [s["message"] for s in _steps(output_file)] == ["first answer"]
    assert _observations(output_file) == []

    _write(input_file, measured_turn + in_flight + [_token_count(input_tokens=300, output_tokens=30)])
    common_transcript_convert.convert(str(input_file), str(output_file))
    steps = _steps(output_file)
    assert len(steps) == 2
    assert steps[1]["metrics"]["prompt_tokens"] == 300
    # The held-back step's own tool result rides out with it, so the observation
    # never precedes the call it belongs to.
    assert _observations(output_file)[0]["results"][0]["source_call_id"] == "call-1"
    _assert_all_conform(_records(output_file))


def test_turn_end_flush_emits_the_trailing_unmeasured_step(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [_assistant("first"), _token_count(input_tokens=100, output_tokens=10), _assistant("still unmeasured")],
    )
    common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)
    assert [s["message"] for s in _steps(output_file)] == ["first", "still unmeasured"]


def test_complete_input_still_holds_a_call_step_whose_tool_is_running(tmp_path: Path) -> None:
    # codex writes an inference's token_count only after its calls' outputs, and a long-running
    # tool leaves the rollout silent. A pass that sees no growth must not release the call step,
    # or the usage that lands once the tool finishes finds the step already emitted.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    measured_turn = [_assistant("first answer"), _token_count(input_tokens=100, output_tokens=10)]
    running_call = _function_call("shell", '{"cmd":"make test"}', "call-1")
    _write(input_file, measured_turn + [running_call])
    common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)
    assert [s["message"] for s in _steps(output_file)] == ["first answer"]

    finished = [_function_call_output("call-1", "ok"), _token_count(input_tokens=300, output_tokens=30)]
    _write(input_file, measured_turn + [running_call] + finished)
    common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)
    steps = _steps(output_file)
    assert steps[1]["tool_calls"][0]["tool_call_id"] == "call-1"
    assert steps[1]["metrics"]["prompt_tokens"] == 300
    _assert_all_conform(_records(output_file))


def test_complete_input_releases_a_call_step_once_its_output_landed_without_usage(tmp_path: Path) -> None:
    # An interrupted call gets an "aborted" output and no token_count; with nothing left pending,
    # the complete pass emits the step unmeasured rather than stranding it.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [
            _assistant("first answer"),
            _token_count(input_tokens=100, output_tokens=10),
            _function_call("shell", '{"cmd":"make test"}', "call-1"),
            _function_call_output("call-1", "aborted by user after 4.4s"),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)
    steps = _steps(output_file)
    assert len(steps) == 2
    assert "metrics" not in steps[1]
    assert _observations(output_file)[0]["results"][0]["source_call_id"] == "call-1"


def test_a_rollout_that_reports_no_usage_holds_nothing_back(tmp_path: Path) -> None:
    # A codex build that never writes token_count has nothing to wait for, so its
    # steps must stream out at the usual pace rather than being held indefinitely.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(input_file, [_user("hello"), _assistant("hi back")])
    common_transcript_convert.convert(str(input_file), str(output_file))
    assert [s["message"] for s in _steps(output_file)] == ["hello", "hi back"]


def test_a_sessions_first_inference_is_held_before_any_token_count_landed(tmp_path: Path) -> None:
    # A live rollout has no token_count until its first inference finishes, after that
    # inference's tool outputs. Its turn_context already says usage is coming, so a pass that
    # runs mid-inference must hold the call rather than emit it before its usage.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    first_inference = [_turn_context("gpt-5.6-sol"), _user("run the tests"), _function_call("shell", "{}", "call-1")]
    _write(input_file, first_inference)
    common_transcript_convert.convert(str(input_file), str(output_file))
    assert [s["source"] for s in _steps(output_file)] == ["user"]

    finished = [_function_call_output("call-1", "ok"), _token_count(input_tokens=500, output_tokens=5)]
    _write(input_file, first_inference + finished)
    common_transcript_convert.convert(str(input_file), str(output_file))
    assert _steps(output_file)[1]["metrics"]["prompt_tokens"] == 500
    _assert_all_conform(_records(output_file))


def test_content_free_token_usage_is_not_reported_as_zeros(tmp_path: Path) -> None:
    # A token_count carrying no recognizable bucket describes nothing; reporting it
    # as an all-zero row would price the inference as free.
    assert common_transcript_convert._closing_metrics({"last_token_usage": {}}) is None
    assert common_transcript_convert._closing_metrics({"last_token_usage": "not-a-dict"}) is None
    assert common_transcript_convert._closing_metrics(None) is None


def test_real_0146_rollout_prices_and_attributes_every_inference(tmp_path: Path) -> None:
    # The real rollout ran two inferences (a tool call, then the closing message);
    # each must carry the turn's model and exactly one inference's worth of usage.
    output_file = tmp_path / "out.jsonl"
    common_transcript_convert.convert(str(_REAL_0146_ROLLOUT), str(output_file))

    agent_steps = [s for s in _steps(output_file) if s["source"] == "agent"]
    assert all(s["model_name"] == "gpt-5.6-sol" for s in agent_steps)
    assert all(s["reasoning_effort"] == "medium" for s in agent_steps)
    measured = [s for s in agent_steps if "metrics" in s]
    assert len(measured) == 2
    # The first inference's usage lands on its tool call, not on the message that
    # preceded it in the same inference.
    assert measured[0]["tool_calls"][0]["function_name"] == "exec"
    assert measured[0]["metrics"] == {
        "prompt_tokens": 21199,
        "completion_tokens": 124,
        "cached_tokens": 0,
        "extra": {"cache_creation_input_tokens": 21196, "reasoning_output_tokens": 19},
    }
    assert measured[1]["metrics"] == {
        "prompt_tokens": 21466,
        "completion_tokens": 49,
        "cached_tokens": 21196,
        "extra": {"cache_creation_input_tokens": 267, "reasoning_output_tokens": 24},
    }
    # The strongest check there is: the per-step metrics summed (which is exactly
    # what the doc-builder's final_metrics does) must reproduce the running session
    # total codex itself reported on the rollout's last token_count.
    codex_total = _last_total_token_usage(_REAL_0146_ROLLOUT)
    assert sum(s["metrics"]["prompt_tokens"] for s in measured) == codex_total["input_tokens"]
    assert sum(s["metrics"]["completion_tokens"] for s in measured) == codex_total["output_tokens"]
    assert sum(s["metrics"]["cached_tokens"] for s in measured) == codex_total["cached_input_tokens"]
    _assert_all_conform(_records(output_file))


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


def _command_executions(output_file: Path) -> list[tuple[str, dict[str, Any]]]:
    """Each command record's owning call id and its command, in stream order."""
    return [
        (result["source_call_id"], result["extra"]["command_execution"])
        for record in _observations(output_file)
        for result in record["results"]
        if result.get("extra", {}).get("tool_name") == "command_execution"
    ]


def test_real_0154_rollout_attaches_every_command_to_the_program_that_ran_it(tmp_path: Path) -> None:
    output_file = tmp_path / "out.jsonl"
    common_transcript_convert.convert(str(_REAL_0154_ROLLOUT), str(output_file), is_input_complete=True)

    exec_call_ids = [
        call["tool_call_id"]
        for step in _steps(output_file)
        for call in step.get("tool_calls") or []
        if call["function_name"] == "exec"
    ]
    commands = _command_executions(output_file)
    assert [(owner, command["cmd"], command["status"], command["exit_code"]) for owner, command in commands] == [
        (exec_call_ids[0], "echo alpha", "completed", 0),
        (exec_call_ids[0], "ls -1 /definitely/missing/path", "failed", 1),
        (exec_call_ids[0], "printf beta", "completed", 0),
        # The program built this one from a JavaScript variable; the record has what ran.
        (exec_call_ids[0], "echo gamma", "completed", 0),
        # Started by the program that yielded, and killed when the turn ended after its wait.
        (exec_call_ids[2], "sleep 20 && echo slow-done", "failed", -1),
    ]
    assert commands[1][1]["output"] == "ls: /definitely/missing/path: No such file or directory\n"
    assert commands[0][1]["cwd"] == "/home/user/workspace"
    assert commands[0][1]["argv"] == ["/bin/zsh", "-lc", "echo alpha"]
    assert all(command["attribution"] == "running_program" for _owner, command in commands)
    # Reading the commands takes nothing from the model and usage stamping on the same rollout.
    agent_steps = [s for s in _steps(output_file) if s["source"] == "agent"]
    assert all(s["model_name"] == "gpt-5.6-terra" for s in agent_steps)
    assert sum(1 for s in agent_steps if "metrics" in s) == 5
    _assert_all_conform(_records(output_file))


def test_a_command_record_follows_its_programs_output_and_carries_no_content(tmp_path: Path) -> None:
    # codex writes a command's item_completed before the program's own output line. The command
    # record still lands after the program's result, so that result stays the first one attached
    # to the call, and it carries no content for a reader of result text to count twice.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [
            _custom_tool_call(
                "exec", 'text(tools.exec_command({cmd: "echo hi"}));', "call-1", "2026-06-09T07:00:01.000Z"
            ),
            _command_execution("echo hi", "2026-06-09T07:00:01.100Z", "2026-06-09T07:00:01.200Z", output="hi\n"),
            _custom_tool_call_output("call-1", "Script completed\nOutput:\nhi\n", "2026-06-09T07:00:01.300Z"),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)

    results = [result for record in _observations(output_file) for result in record["results"]]
    assert [result["extra"]["tool_name"] for result in results] == ["exec", "command_execution"]
    assert results[1]["source_call_id"] == "call-1"
    assert results[1]["content"] == ""
    assert results[1]["extra"]["command_execution"]["output"] == "hi\n"
    _assert_all_conform(_records(output_file))


def test_a_command_that_finishes_after_its_program_answered_is_appended_on_a_later_pass(tmp_path: Path) -> None:
    # A program that yields answers with a cell id while its command keeps running, so the command's
    # record can arrive after the program's output has already been streamed.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    call = _custom_tool_call("exec", 'tools.exec_command({cmd: "sleep 5"});', "call-1", "2026-06-09T07:00:01.000Z")
    yielded = _custom_tool_call_output(
        "call-1", "Script running with cell ID 7\nOutput:\n", "2026-06-09T07:00:01.300Z"
    )
    _write(input_file, [call, yielded])
    common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)
    assert _command_executions(output_file) == []

    finished = _command_execution("sleep 5", "2026-06-09T07:00:01.200Z", "2026-06-09T07:00:06.200Z")
    _write(input_file, [call, yielded, finished])
    assert common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True) == 1
    assert [(owner, command["cmd"]) for owner, command in _command_executions(output_file)] == [("call-1", "sleep 5")]
    assert common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True) == 0


def test_two_running_programs_are_told_apart_by_the_command_each_names(tmp_path: Path) -> None:
    # A program that yielded is still running when the model starts another, so a command started
    # then could belong to either; the program whose own text names the command owns it.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [
            _custom_tool_call(
                "exec", 'tools.exec_command({cmd: "npm run build"});', "call-a", "2026-06-09T07:00:01.000Z"
            ),
            _custom_tool_call_output("call-a", "Script running with cell ID 1\nOutput:\n", "2026-06-09T07:00:01.500Z"),
            _custom_tool_call(
                "exec", 'tools.exec_command({cmd: "git status"});', "call-b", "2026-06-09T07:00:02.000Z"
            ),
            _command_execution("git status", "2026-06-09T07:00:02.100Z", "2026-06-09T07:00:02.200Z", item_id="exec-b"),
            _custom_tool_call_output("call-b", "Script completed\nOutput:\n", "2026-06-09T07:00:02.300Z"),
            _function_call("wait", '{"cell_id": "1"}', "call-w", "2026-06-09T07:00:03.000Z"),
            _command_execution(
                "npm run build", "2026-06-09T07:00:01.100Z", "2026-06-09T07:00:03.400Z", item_id="exec-a"
            ),
            _function_call_output("call-w", "Script completed\nOutput:\n", "2026-06-09T07:00:03.500Z"),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)

    assert [
        (owner, command["cmd"], command["attribution"]) for owner, command in _command_executions(output_file)
    ] == [
        ("call-b", "git status", "named_in_program"),
        ("call-a", "npm run build", "running_program"),
    ]


def test_a_command_no_running_program_names_goes_to_the_latest_of_them_and_says_so(tmp_path: Path) -> None:
    # A command built at run time is spelled out by neither program, so time alone decides.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [
            _custom_tool_call("exec", "tools.exec_command({cmd: first});", "call-a", "2026-06-09T07:00:01.000Z"),
            _custom_tool_call_output("call-a", "Script running with cell ID 1\nOutput:\n", "2026-06-09T07:00:01.500Z"),
            _custom_tool_call("exec", "tools.exec_command({cmd: second});", "call-b", "2026-06-09T07:00:02.000Z"),
            _command_execution("make test", "2026-06-09T07:00:02.100Z", "2026-06-09T07:00:02.200Z"),
            _custom_tool_call_output("call-b", "Script completed\nOutput:\n", "2026-06-09T07:00:02.300Z"),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)

    assert [(owner, command["attribution"]) for owner, command in _command_executions(output_file)] == [
        ("call-b", "latest_running_program")
    ]


def test_a_commands_output_copy_is_clipped_and_its_full_length_recorded(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    long_output = "x" * (common_transcript_convert._COMMAND_OUTPUT_CHAR_LIMIT + 10)
    _write(
        input_file,
        [
            _custom_tool_call(
                "exec", 'tools.exec_command({cmd: "cat big.log"});', "call-1", "2026-06-09T07:00:01.000Z"
            ),
            _command_execution(
                "cat big.log", "2026-06-09T07:00:01.100Z", "2026-06-09T07:00:01.200Z", output=long_output
            ),
            _custom_tool_call_output("call-1", "Script completed\nOutput:\n", "2026-06-09T07:00:01.300Z"),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)

    command = _command_executions(output_file)[0][1]
    assert command["output"] == long_output[: common_transcript_convert._COMMAND_OUTPUT_CHAR_LIMIT]
    assert command["output_length"] == len(long_output)


def test_real_0146_rollout_carries_no_command_records(tmp_path: Path) -> None:
    # codex 0.146 and 0.147 write the legacy history mode, which has no CommandExecution items.
    output_file = tmp_path / "out.jsonl"
    common_transcript_convert.convert(str(_REAL_0146_ROLLOUT), str(output_file))
    assert _command_executions(output_file) == []


def test_command_text_is_the_shell_script_or_the_quoted_argv() -> None:
    assert common_transcript_convert._command_text(["/bin/zsh", "-lc", "echo a && echo b"]) == "echo a && echo b"
    assert common_transcript_convert._command_text(["git", "commit", "-m", "two words"]) == "git commit -m 'two words'"
    assert common_transcript_convert._command_text("not an argv") == ""


def test_rollout_timestamps_are_read_as_utc_milliseconds_and_unreadable_ones_are_refused() -> None:
    assert common_transcript_convert._epoch_ms("2026-06-09T07:00:01.250Z") == _timestamp_ms("2026-06-09T07:00:01.250Z")
    # A timestamp without an offset is read as UTC, which is what codex writes.
    assert common_transcript_convert._epoch_ms("2026-06-09T07:00:01.250") == _timestamp_ms("2026-06-09T07:00:01.250Z")
    assert common_transcript_convert._epoch_ms("not a time") is None
    assert common_transcript_convert._epoch_ms("") is None
    assert common_transcript_convert._epoch_ms(None) is None


def test_a_commands_duration_and_directory_degrade_rather_than_fail_on_malformed_fields() -> None:
    assert common_transcript_convert._duration_ms({"secs": 1, "nanos": 250_000_000}) == 1250
    assert common_transcript_convert._duration_ms({"secs": 1}) is None
    assert common_transcript_convert._duration_ms("1.25s") is None
    assert common_transcript_convert._local_path("file:///home/user/my%20dir") == "/home/user/my dir"
    assert common_transcript_convert._local_path("/home/user/workspace") == "/home/user/workspace"
    assert common_transcript_convert._local_path(None) == ""


def test_an_empty_command_is_never_named_by_a_program() -> None:
    # Every program text contains the empty string, so matching on it would name every running
    # program at once.
    assert common_transcript_convert._program_names_command('tools.exec_command({cmd: ""});', "") is False


def test_a_command_no_call_can_own_is_not_attached(tmp_path: Path) -> None:
    # One command started before any program ran, and one lost its start time. Neither can be
    # placed on a call, so neither is attached to a guessed one.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    before_any_program = _command_execution(
        "echo early", "2026-06-09T07:00:00.500Z", "2026-06-09T07:00:00.600Z", item_id="exec-early"
    )
    no_start_time = _command_execution(
        "echo unknown", "2026-06-09T07:00:01.100Z", "2026-06-09T07:00:01.200Z", item_id="exec-unknown"
    )
    del no_start_time["payload"]["started_at_ms"]
    _write(
        input_file,
        [
            before_any_program,
            _custom_tool_call(
                "exec", 'tools.exec_command({cmd: "echo unknown"});', "call-1", "2026-06-09T07:00:01.000Z"
            ),
            no_start_time,
            _custom_tool_call_output("call-1", "Script completed\nOutput:\n", "2026-06-09T07:00:01.300Z"),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)

    assert _command_executions(output_file) == []
    assert [result["source_call_id"] for record in _observations(output_file) for result in record["results"]] == [
        "call-1"
    ]


def test_a_command_started_after_every_program_finished_goes_to_the_nearest_call_before_it(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [
            _custom_tool_call("exec", 'tools.exec_command({cmd: "make"});', "call-1", "2026-06-09T07:00:01.000Z"),
            _custom_tool_call_output("call-1", "Script completed\nOutput:\n", "2026-06-09T07:00:01.300Z"),
            _command_execution("make", "2026-06-09T07:00:02.000Z", "2026-06-09T07:00:02.500Z"),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)

    assert [(owner, command["attribution"]) for owner, command in _command_executions(output_file)] == [
        ("call-1", "nearest_preceding_call")
    ]


def test_an_unknown_turn_never_counts_as_the_commands_turn() -> None:
    # A rollout without turn ids leaves both the calls' and the command's turn empty, and two
    # unknown turns are not the same turn, so only the plain timing rules decide.
    running_windows = {
        "call-a": {"started_ms": 1000, "finished_ms": None, "program": "first", "turn_id": ""},
        "call-b": {"started_ms": 2000, "finished_ms": None, "program": "second", "turn_id": ""},
    }
    finished_windows = {"call-1": {"started_ms": 1000, "finished_ms": 1300, "program": "first", "turn_id": ""}}
    assert common_transcript_convert._owning_call(2100, "make", "", running_windows) == (
        "call-b",
        "latest_running_program",
    )
    assert common_transcript_convert._owning_call(2100, "make", "", finished_windows) == (
        "call-1",
        "nearest_preceding_call",
    )


def test_a_commands_turn_never_overrides_the_program_running_when_it_started() -> None:
    # The only program running when the command started was made in another turn; an earlier call
    # of the command's own turn had already finished, so time alone decides.
    windows = {
        "call-1": {"started_ms": 1000, "finished_ms": 1300, "program": "first", "turn_id": "turn-1"},
        "call-2": {"started_ms": 2000, "finished_ms": None, "program": "second", "turn_id": "turn-2"},
    }
    assert common_transcript_convert._owning_call(2100, "make", "turn-1", windows) == ("call-2", "running_program")


def test_programs_that_name_a_command_outrank_one_that_does_not_even_when_several_do() -> None:
    # Two running programs run the same command and a third, started last, spells out neither, so
    # naming alone cannot pick one; the turn and start order then choose among the two that name it.
    running_windows = {
        "call-a": {"started_ms": 1000, "finished_ms": None, "program": '"npm test"', "turn_id": "turn-1"},
        "call-b": {"started_ms": 2000, "finished_ms": None, "program": '"npm test"', "turn_id": "turn-2"},
        "call-c": {"started_ms": 3000, "finished_ms": None, "program": "script", "turn_id": "turn-2"},
    }
    assert common_transcript_convert._owning_call(3100, "npm test", "turn-1", running_windows) == (
        "call-a",
        "latest_running_program_in_turn",
    )
    assert common_transcript_convert._owning_call(3100, "npm test", "turn-2", running_windows) == (
        "call-b",
        "latest_running_program_in_turn",
    )
    assert common_transcript_convert._owning_call(3100, "npm test", "", running_windows) == (
        "call-b",
        "latest_running_program",
    )


def test_overlapping_programs_from_two_turns_are_told_apart_by_the_commands_turn(tmp_path: Path) -> None:
    # A yielded program no wait collected is still running when the next turn starts a program, and
    # neither spells out the command, so the turn codex recorded each command in decides.
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [
            _turn_context("gpt-5.6-terra", turn_id="turn-1"),
            _custom_tool_call("exec", "tools.exec_command({cmd: first});", "call-a", "2026-06-09T07:00:01.000Z"),
            _custom_tool_call_output("call-a", "Script running with cell ID 1\nOutput:\n", "2026-06-09T07:00:01.500Z"),
            _turn_context("gpt-5.6-terra", turn_id="turn-2"),
            _custom_tool_call("exec", "tools.exec_command({cmd: second});", "call-b", "2026-06-09T07:00:02.000Z"),
            _command_execution(
                "make test", "2026-06-09T07:00:02.100Z", "2026-06-09T07:00:02.200Z", item_id="exec-a", turn_id="turn-1"
            ),
            _command_execution(
                "make lint", "2026-06-09T07:00:02.100Z", "2026-06-09T07:00:02.250Z", item_id="exec-b", turn_id="turn-2"
            ),
            _custom_tool_call_output("call-b", "Script completed\nOutput:\n", "2026-06-09T07:00:02.300Z"),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)

    assert [
        (owner, command["cmd"], command["attribution"]) for owner, command in _command_executions(output_file)
    ] == [
        ("call-a", "make test", "latest_running_program_in_turn"),
        ("call-b", "make lint", "latest_running_program_in_turn"),
    ]


def test_a_command_started_after_every_program_finished_goes_to_the_nearest_call_of_its_turn(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [
            _turn_context("gpt-5.6-terra", turn_id="turn-1"),
            _custom_tool_call("exec", "tools.exec_command({cmd: first});", "call-1", "2026-06-09T07:00:01.000Z"),
            _custom_tool_call_output("call-1", "Script completed\nOutput:\n", "2026-06-09T07:00:01.300Z"),
            _turn_context("gpt-5.6-terra", turn_id="turn-2"),
            _custom_tool_call("exec", "tools.exec_command({cmd: second});", "call-2", "2026-06-09T07:00:02.000Z"),
            _custom_tool_call_output("call-2", "Script completed\nOutput:\n", "2026-06-09T07:00:02.300Z"),
            _command_execution("make", "2026-06-09T07:00:03.000Z", "2026-06-09T07:00:03.500Z", turn_id="turn-1"),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)

    assert [(owner, command["attribution"]) for owner, command in _command_executions(output_file)] == [
        ("call-1", "nearest_preceding_call_in_turn")
    ]


def test_reconverting_a_rollout_with_measured_reasoning_appends_nothing(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write(
        input_file,
        [
            _turn_context("gpt-5.6-terra"),
            _user("plan it"),
            _reasoning("weighing it"),
            _token_count(input_tokens=9, output_tokens=3),
        ],
    )
    assert common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True) > 0
    content_after_first_pass = output_file.read_text()

    assert common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True) == 0
    assert output_file.read_text() == content_after_first_pass
