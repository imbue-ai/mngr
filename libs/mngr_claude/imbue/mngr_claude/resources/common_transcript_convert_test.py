"""Unit tests for the claude common-transcript converter (common_transcript_convert.py).

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
from imbue.mngr_claude.resources import common_transcript_convert
from imbue.mngr_claude.resources.testing import DEFAULT_PROMPT_TOKENS
from imbue.mngr_claude.resources.testing import make_assistant_record
from imbue.mngr_claude.resources.testing import make_tool_result_record
from imbue.mngr_claude.resources.testing import make_user_record
from imbue.mngr_claude.resources.testing import read_observations
from imbue.mngr_claude.resources.testing import read_steps
from imbue.mngr_claude.resources.testing import read_stream
from imbue.mngr_claude.resources.testing import write_raw_transcript

# A scrubbed slice of real Claude Code session JSONL (captured from actual
# ~/.claude/projects/.../*.jsonl and preserved-agent transcripts on this host,
# claude-code 2.1.207; opaque payloads shortened, structure and markup verbatim).
# It covers, in file order: a noise line, a custom-command expansion
# (<command-message>-led), an isMeta <local-command-caveat> wrapper, a built-in
# /model expansion (<command-name>-led), a <local-command-stdout> confirmation,
# a genuine typed user turn, three assistant lines fanning ONE inference out over
# thinking/text/tool_use, a second inference's tool_use, and their two
# tool_results -- the second of which QUOTES command markup mid-output (the
# over-filtering trap).
_REAL_SESSION_FIXTURE = Path(__file__).parent / "test_fixtures" / "claude_session_slice.jsonl"


def _real_session_lines() -> list[str]:
    return _REAL_SESSION_FIXTURE.read_text().splitlines()


def _convert_complete(input_file: Path, output_file: Path) -> int:
    """Convert an input that is known to be complete (as a turn-end flush does).

    Most tests describe a whole transcript, so nothing is still being appended and
    the last inference is emitted like any other; the deferral tests below drive
    the mid-turn form explicitly.
    """
    return common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)


# -- Header --


def test_header_ids_differ_between_agents() -> None:
    # Analytics dedupes the fleet by event id, so two agents' headers must not collide.
    assert common_transcript_convert._header_event_id("agent-a") != common_transcript_convert._header_event_id(
        "agent-b"
    )


def test_header_is_the_first_line_and_written_once(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(input_file, [make_user_record("u1", text="first")])
    _convert_complete(input_file, output_file)
    write_raw_transcript(
        input_file,
        [
            make_user_record("u1", text="first"),
            make_user_record("u2", text="second", timestamp="2026-01-01T00:00:05Z"),
        ],
    )
    _convert_complete(input_file, output_file)

    events = read_stream(output_file)
    assert events[0] == {
        "type": "header",
        "event_id": common_transcript_convert._header_event_id(""),
        "emitter": "claude/common_transcript",
        "schema_version": "ATIF-v1.7",
    }
    assert [e["type"] for e in events].count("header") == 1


def test_no_output_means_no_header(tmp_path: Path) -> None:
    """A pass with nothing to append must not create a header-only stream."""
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(input_file, [{"type": "progress", "uuid": "u1", "timestamp": "2026-01-01T00:00:00Z"}])
    assert _convert_complete(input_file, output_file) == 0
    assert not output_file.exists()


def test_pinned_schema_version_matches_the_canonical_one() -> None:
    """The converter restates the ATIF revision (it cannot import mngr on the host)."""
    assert common_transcript_convert._SCHEMA_VERSION == PINNED_ATIF_SCHEMA_VERSION


# -- Agent steps --


def test_converts_assistant_message_with_metrics_and_tool_calls(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(
        input_file,
        [make_assistant_record("u1", text="hello", tool_uses=[{"id": "t1", "name": "Bash", "input": {"cmd": "ls"}}])],
    )
    _convert_complete(input_file, output_file)
    step = read_steps(output_file, "agent")[0]
    assert step["event_id"] == "u1-assistant"
    assert step["emitter"] == "claude/common_transcript"
    assert step["model_name"] == "claude-opus-4-8"
    assert step["message"] == "hello"
    assert step["tool_calls"] == [{"tool_call_id": "t1", "function_name": "Bash", "arguments": {"cmd": "ls"}}]
    # ATIF prompt_tokens counts ALL input tokens, cached ones included.
    assert step["metrics"] == {
        "prompt_tokens": DEFAULT_PROMPT_TOKENS,
        "completion_tokens": 50,
        "cached_tokens": 80,
        "extra": {"cache_creation_input_tokens": 20},
    }
    assert step["extra"] == {"finish_reason": "end_turn", "message_id": "msg_u1"}
    # Each group is exactly one API response by construction.
    assert step["llm_call_count"] == 1
    assert validate_common_transcript_record(step) is None


def test_usage_without_cache_counters_omits_the_cache_write_extra(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(
        input_file, [make_assistant_record("u1", text="hi", usage={"input_tokens": 10, "output_tokens": 3})]
    )
    _convert_complete(input_file, output_file)
    assert read_steps(output_file, "agent")[0]["metrics"] == {
        "prompt_tokens": 10,
        "completion_tokens": 3,
        "cached_tokens": 0,
    }


def test_fanned_out_inference_becomes_one_step_with_one_set_of_metrics(tmp_path: Path) -> None:
    """Claude splits one API response across lines sharing message.id; ATIF wants one step."""
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(
        input_file,
        [
            make_assistant_record(
                "u1", text="", thinking="first thought", message_id="msg_1", timestamp="2026-01-01T00:00:01Z"
            ),
            make_assistant_record("u2", text="on it", message_id="msg_1", timestamp="2026-01-01T00:00:02Z"),
            make_assistant_record(
                "u3",
                text="",
                tool_uses=[{"id": "t1", "name": "Bash", "input": {"cmd": "ls"}}],
                message_id="msg_1",
                timestamp="2026-01-01T00:00:03Z",
            ),
        ],
    )
    _convert_complete(input_file, output_file)

    steps = read_steps(output_file, "agent")
    assert len(steps) == 1, steps
    step = steps[0]
    # The step is keyed and timestamped by the inference's FIRST line.
    assert step["event_id"] == "u1-assistant"
    assert step["timestamp"] == "2026-01-01T00:00:01Z"
    assert step["message"] == "on it"
    assert step["reasoning_content"] == "first thought"
    assert [call["tool_call_id"] for call in step["tool_calls"]] == ["t1"]
    # The usage repeats identically on every line; counting it once is the point.
    assert step["metrics"]["prompt_tokens"] == DEFAULT_PROMPT_TOKENS
    assert step["llm_call_count"] == 1
    assert step["metrics"]["completion_tokens"] == 50


def test_multiple_thinking_blocks_are_joined_with_a_blank_line(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(
        input_file,
        [
            make_assistant_record(
                "u1", text="", thinking="first", message_id="msg_1", timestamp="2026-01-01T00:00:01Z"
            ),
            make_assistant_record(
                "u2", text="", thinking="second", message_id="msg_1", timestamp="2026-01-01T00:00:02Z"
            ),
        ],
    )
    _convert_complete(input_file, output_file)
    assert read_steps(output_file, "agent")[0]["reasoning_content"] == "first\n\nsecond"


def test_parallel_tool_calls_interleaved_with_their_results_stay_one_step(tmp_path: Path) -> None:
    """Claude writes each parallel tool_use line only after the previous result lands.

    The lines of one inference are therefore separated by tool-result-only user
    records, which must not split the inference into two steps (which would also
    double-count its usage).
    """
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(
        input_file,
        [
            make_assistant_record(
                "u1",
                text="working",
                tool_uses=[{"id": "t1", "name": "Bash", "input": {"cmd": "ls"}}],
                message_id="msg_1",
                timestamp="2026-01-01T00:00:01Z",
            ),
            make_tool_result_record("u2", "t1", "one", timestamp="2026-01-01T00:00:02Z"),
            make_assistant_record(
                "u3",
                text="",
                tool_uses=[{"id": "t2", "name": "Read", "input": {"file": "a"}}],
                message_id="msg_1",
                timestamp="2026-01-01T00:00:03Z",
            ),
            make_tool_result_record("u4", "t2", "two", timestamp="2026-01-01T00:00:04Z"),
        ],
    )
    _convert_complete(input_file, output_file)

    steps = read_steps(output_file, "agent")
    assert len(steps) == 1, steps
    assert [call["tool_call_id"] for call in steps[0]["tool_calls"]] == ["t1", "t2"]
    assert steps[0]["metrics"]["prompt_tokens"] == DEFAULT_PROMPT_TOKENS
    assert [o["results"][0]["source_call_id"] for o in read_observations(output_file)] == ["t1", "t2"]


def test_a_new_message_id_starts_a_new_step(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(
        input_file,
        [
            make_assistant_record("u1", text="one", message_id="msg_1", timestamp="2026-01-01T00:00:01Z"),
            make_assistant_record("u2", text="two", message_id="msg_2", timestamp="2026-01-01T00:00:02Z"),
        ],
    )
    _convert_complete(input_file, output_file)
    steps = read_steps(output_file, "agent")
    assert [s["message"] for s in steps] == ["one", "two"]
    assert [s["event_id"] for s in steps] == ["u1-assistant", "u2-assistant"]
    assert [s["extra"]["message_id"] for s in steps] == ["msg_1", "msg_2"]


def test_content_free_assistant_message_emits_no_step(tmp_path: Path) -> None:
    """A response with nothing said, thought, or done would render as an empty turn."""
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(
        input_file,
        [
            {
                "type": "assistant",
                "uuid": "u1",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {"id": "msg_1", "content": [{"type": "redacted_thinking", "data": "opaque"}]},
            }
        ],
    )
    assert _convert_complete(input_file, output_file) == 0


# -- Fidelity --


def test_large_tool_input_survives_untruncated(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    large_input = {"content": "x" * 5000, "path": "/tmp/f", "nested": {"deep": ["a"] * 100}}
    write_raw_transcript(
        input_file,
        [make_assistant_record("u1", text="", tool_uses=[{"id": "t1", "name": "Write", "input": large_input}])],
    )
    _convert_complete(input_file, output_file)
    assert read_steps(output_file, "agent")[0]["tool_calls"][0]["arguments"] == large_input


def test_large_tool_output_survives_untruncated(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    large_output = "y" * 20000
    write_raw_transcript(
        input_file,
        [
            make_assistant_record("u1", text="", tool_uses=[{"id": "t1", "name": "Bash"}]),
            make_tool_result_record("u2", "t1", large_output),
        ],
    )
    _convert_complete(input_file, output_file)
    assert read_observations(output_file)[0]["results"][0]["content"] == large_output


def test_large_system_step_text_survives_untruncated(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    feedback = "Stop hook feedback:\n" + "z" * 20000
    write_raw_transcript(input_file, [make_user_record("u1", text=feedback, is_meta=True)])
    _convert_complete(input_file, output_file)
    assert read_steps(output_file, "system")[0]["message"] == feedback


def test_non_object_tool_input_is_wrapped_rather_than_dropped(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(
        input_file,
        [
            {
                "type": "assistant",
                "uuid": "u1",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "id": "msg_1",
                    "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": "ls -la"}],
                },
            }
        ],
    )
    _convert_complete(input_file, output_file)
    assert read_steps(output_file, "agent")[0]["tool_calls"][0]["arguments"] == {"_raw": "ls -la"}


def test_images_become_a_text_placeholder(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    image_block = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    write_raw_transcript(
        input_file,
        [
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "look:"}, image_block]},
            },
            make_assistant_record("u2", text="", tool_uses=[{"id": "t1", "name": "Read"}]),
            make_tool_result_record("u3", "t1", [image_block]),
        ],
    )
    _convert_complete(input_file, output_file)
    assert read_steps(output_file, "user")[0]["message"] == "look:\n[image omitted]"
    assert read_observations(output_file)[0]["results"][0]["content"] == "[image omitted]"


# -- User, system and compaction steps --


def test_converts_user_text_message(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(input_file, [make_user_record("u1", text="hi there")])
    _convert_complete(input_file, output_file)
    step = read_steps(output_file, "user")[0]
    assert step["message"] == "hi there"
    assert step["event_id"] == "u1-user"
    # The ATIF Step model rejects agent-only fields on a user step.
    assert "model_name" not in step and "metrics" not in step


def test_meta_user_message_is_a_system_step(tmp_path: Path) -> None:
    """Framework-injected content (stop hook output) is a system step, not a user turn."""
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(input_file, [make_user_record("u1", text="stop hook output", is_meta=True)])
    _convert_complete(input_file, output_file)
    step = read_steps(output_file, "system")[0]
    assert step["message"] == "stop hook output"
    assert step["event_id"] == "u1-meta"
    assert read_steps(output_file, "user") == []


def test_compaction_becomes_a_system_step_carrying_the_summary(tmp_path: Path) -> None:
    """Claude marks the post-compaction context recap with isCompactSummary=true."""
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    summary = "This session is being continued from a previous conversation...\n\nSummary:\n1. Primary Request"
    write_raw_transcript(
        input_file,
        [
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-01-01T00:00:00Z",
                "isCompactSummary": True,
                "isVisibleInTranscriptOnly": True,
                "message": {"role": "user", "content": summary},
            }
        ],
    )
    _convert_complete(input_file, output_file)
    step = read_steps(output_file, "system")[0]
    assert step["event_id"] == "u1-compact"
    assert step["message"] == "Context compaction performed"
    assert step["extra"] == {"context_management": {"type": "compaction", "boundary": "replace"}}
    # A system step already has its result, so the summary rides inline.
    assert step["observation"] == {"results": [{"content": summary}]}
    assert validate_common_transcript_record(step) is None
    assert read_steps(output_file, "user") == []


# -- Observations --


def test_tool_result_becomes_an_observation_labeled_from_its_call(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(
        input_file,
        [
            make_assistant_record("u1", text="", tool_uses=[{"id": "t1", "name": "Read"}]),
            make_tool_result_record("u2", "t1", [{"type": "text", "text": "file contents"}]),
        ],
    )
    _convert_complete(input_file, output_file)
    observation = read_observations(output_file)[0]
    assert observation["event_id"] == "u2-tool_result-t1"
    assert observation["results"] == [
        {"source_call_id": "t1", "content": "file contents", "extra": {"is_error": False, "tool_name": "Read"}}
    ]


def test_tool_result_error_flag_is_carried(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(
        input_file,
        [
            make_assistant_record("u1", text="", tool_uses=[{"id": "t1", "name": "Bash"}]),
            make_tool_result_record("u2", "t1", "boom", is_error=True),
        ],
    )
    _convert_complete(input_file, output_file)
    assert read_observations(output_file)[0]["results"][0]["extra"] == {"is_error": True, "tool_name": "Bash"}


def test_unknown_tool_name_falls_back(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(input_file, [make_tool_result_record("u1", "orphan", "result")])
    _convert_complete(input_file, output_file)
    assert read_observations(output_file)[0]["results"][0]["extra"]["tool_name"] == "unknown"


def test_user_message_with_text_and_tool_results_emits_both(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(
        input_file,
        [
            make_assistant_record("u1", text="", tool_uses=[{"id": "t1", "name": "Edit"}]),
            {
                "type": "user",
                "uuid": "u2",
                "timestamp": "2026-01-01T00:00:02Z",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Continue please"},
                        {"type": "tool_result", "tool_use_id": "t1", "content": "done"},
                    ],
                },
            },
        ],
    )
    _convert_complete(input_file, output_file)
    assert read_steps(output_file, "user")[0]["message"] == "Continue please"
    assert read_observations(output_file)[0]["results"][0]["source_call_id"] == "t1"


# -- Trailing-group deferral --


def test_trailing_assistant_group_is_deferred_until_a_later_line_closes_it(tmp_path: Path) -> None:
    """Mid-turn the last inference is still being appended to, line by line.

    Emitting it early would freeze it half-written: the output is deduped by
    event_id, so the missing blocks could never be added.
    """
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(
        input_file,
        [make_user_record("u1", text="go"), make_assistant_record("u2", text="partial", message_id="msg_1")],
    )
    common_transcript_convert.convert(str(input_file), str(output_file))
    assert read_steps(output_file, "agent") == []
    assert [e["message"] for e in read_steps(output_file, "user")] == ["go"]

    # The rest of the inference lands, then the next turn's user message closes it.
    write_raw_transcript(
        input_file,
        [
            make_user_record("u1", text="go"),
            make_assistant_record("u2", text="partial", message_id="msg_1"),
            make_assistant_record(
                "u3",
                text="",
                tool_uses=[{"id": "t1", "name": "Bash"}],
                message_id="msg_1",
                timestamp="2026-01-01T00:00:03Z",
            ),
            make_user_record("u4", text="thanks", timestamp="2026-01-01T00:00:04Z"),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file))
    steps = read_steps(output_file, "agent")
    assert len(steps) == 1
    assert steps[0]["message"] == "partial"
    assert [call["tool_call_id"] for call in steps[0]["tool_calls"]] == ["t1"]


def test_flush_emits_the_trailing_group_and_a_later_pass_does_not_duplicate_it(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(input_file, [make_user_record("u1", text="go"), make_assistant_record("u2", text="done")])
    _convert_complete(input_file, output_file)
    assert [s["message"] for s in read_steps(output_file, "agent")] == ["done"]

    # A later daemon pass over the same input re-reads everything; dedup by
    # event_id must keep the already-flushed inference from being appended twice.
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 0
    assert _convert_complete(input_file, output_file) == 0
    assert [s["message"] for s in read_steps(output_file, "agent")] == ["done"]


def test_results_of_a_deferred_inference_wait_for_it(tmp_path: Path) -> None:
    """An output must never appear in the stream before the call that produced it."""
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(
        input_file,
        [
            make_assistant_record("u1", text="", tool_uses=[{"id": "t1", "name": "Bash"}]),
            make_tool_result_record("u2", "t1", "output"),
        ],
    )
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 0

    assert _convert_complete(input_file, output_file) > 0
    assert [e["type"] for e in read_stream(output_file)] == ["header", "step", "observation"]


def test_a_result_of_an_already_closed_inference_is_not_deferred(tmp_path: Path) -> None:
    """Only the OPEN inference holds its results back, not every in-flight call."""
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(
        input_file,
        [
            make_assistant_record(
                "u1",
                text="",
                tool_uses=[{"id": "t1", "name": "Bash"}],
                message_id="msg_1",
                timestamp="2026-01-01T00:00:01Z",
            ),
            make_assistant_record("u2", text="next", message_id="msg_2", timestamp="2026-01-01T00:00:02Z"),
            make_tool_result_record("u3", "t1", "late result", timestamp="2026-01-01T00:00:03Z"),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file))
    assert [s["event_id"] for s in read_steps(output_file, "agent")] == ["u1-assistant"]
    assert [o["results"][0]["source_call_id"] for o in read_observations(output_file)] == ["t1"]


# -- Sidechain (native Task subagent) lane --


def _main_thread_inference_around_a_sidechain_turn() -> list[dict[str, Any]]:
    """One main-thread inference whose two tool_use lines straddle a subagent turn.

    Claude runs a native Task subagent by writing its records into the same session
    file, marked isSidechain, between the lines of the inference that dispatched it.
    """
    return [
        make_assistant_record(
            "m1",
            text="dispatching",
            tool_uses=[{"id": "t1", "name": "Task", "input": {"prompt": "look"}}],
            message_id="msg_main",
            timestamp="2026-01-01T00:00:01Z",
        ),
        make_user_record("s1", text="look into the logs", is_sidechain=True, timestamp="2026-01-01T00:00:02Z"),
        make_assistant_record(
            "s2",
            text="found it",
            message_id="msg_side",
            is_sidechain=True,
            timestamp="2026-01-01T00:00:03Z",
        ),
        make_assistant_record(
            "m2",
            text="",
            tool_uses=[{"id": "t2", "name": "Read", "input": {"file": "a"}}],
            message_id="msg_main",
            timestamp="2026-01-01T00:00:04Z",
        ),
        make_user_record("m3", text="thanks", timestamp="2026-01-01T00:00:05Z"),
    ]


def test_a_sidechain_turn_does_not_split_the_main_thread_inference(tmp_path: Path) -> None:
    """A subagent's records share the file with the main thread but not its grouping.

    Grouped as one lane, the subagent's prompt would close the main thread's
    in-flight inference: two steps for one API response, with its usage counted
    twice.
    """
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(input_file, _main_thread_inference_around_a_sidechain_turn())
    _convert_complete(input_file, output_file)

    main_steps = [s for s in read_steps(output_file, "agent") if "is_sidechain" not in s["extra"]]
    assert len(main_steps) == 1, main_steps
    assert [call["tool_call_id"] for call in main_steps[0]["tool_calls"]] == ["t1", "t2"]
    assert main_steps[0]["metrics"]["prompt_tokens"] == DEFAULT_PROMPT_TOKENS
    assert main_steps[0]["llm_call_count"] == 1


def test_sidechain_records_carry_their_provenance(tmp_path: Path) -> None:
    """Every sidechain-lane record is marked, so a consumer can carve the subagent out."""
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(
        input_file,
        _main_thread_inference_around_a_sidechain_turn()
        + [
            make_assistant_record(
                "s3",
                text="",
                tool_uses=[{"id": "t3", "name": "Grep"}],
                message_id="msg_side_2",
                is_sidechain=True,
                timestamp="2026-01-01T00:00:06Z",
            ),
            make_tool_result_record("s4", "t3", "a match", is_sidechain=True, timestamp="2026-01-01T00:00:07Z"),
        ],
    )
    _convert_complete(input_file, output_file)

    sidechain_agent_steps = [s for s in read_steps(output_file, "agent") if s["extra"].get("is_sidechain")]
    assert [s["message"] for s in sidechain_agent_steps] == ["found it", ""]
    sidechain_user_steps = [s for s in read_steps(output_file, "user") if s.get("extra", {}).get("is_sidechain")]
    assert [s["message"] for s in sidechain_user_steps] == ["look into the logs"]
    # The main thread's own turns stay unmarked.
    assert [s["message"] for s in read_steps(output_file, "user") if "extra" not in s] == ["thanks"]
    assert [o["results"][0]["extra"].get("is_sidechain") for o in read_observations(output_file)] == [True]
    for event in read_stream(output_file):
        assert validate_common_transcript_record(event) is None, event


def test_each_lane_defers_its_own_trailing_inference(tmp_path: Path) -> None:
    """Mid-turn both conversations are still being appended to, independently."""
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(
        input_file,
        [
            make_assistant_record("m1", text="main closed", message_id="msg_1", timestamp="2026-01-01T00:00:01Z"),
            make_user_record("m2", text="go on", timestamp="2026-01-01T00:00:02Z"),
            make_assistant_record("m3", text="main open", message_id="msg_2", timestamp="2026-01-01T00:00:03Z"),
            make_user_record("s1", text="sub prompt", is_sidechain=True, timestamp="2026-01-01T00:00:04Z"),
            make_assistant_record(
                "s2", text="sub open", message_id="msg_3", is_sidechain=True, timestamp="2026-01-01T00:00:05Z"
            ),
        ],
    )
    common_transcript_convert.convert(str(input_file), str(output_file))
    # Neither lane's trailing inference is emitted: a record of one lane never
    # proves the other lane's inference is finished.
    assert [s["message"] for s in read_steps(output_file, "agent")] == ["main closed"]

    assert _convert_complete(input_file, output_file) > 0
    assert [s["message"] for s in read_steps(output_file, "agent")] == ["main closed", "main open", "sub open"]


# -- Robustness --


def test_dedup_against_existing_output(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(input_file, [make_assistant_record("u1", text="hi")])
    # The header plus the one step.
    assert _convert_complete(input_file, output_file) == 2
    # Re-running over the same input must not re-append (ID-based dedup).
    assert _convert_complete(input_file, output_file) == 0
    assert len(read_stream(output_file)) == 2


def test_malformed_input_line_is_skipped(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(input_file, ["{not json", make_user_record("u1", text="real message")])
    _convert_complete(input_file, output_file)
    assert [e["message"] for e in read_steps(output_file, "user")] == ["real message"]


def test_corrupt_existing_output_line_is_skipped(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    output_file.write_text("{corrupt existing line\n")
    write_raw_transcript(input_file, [make_user_record("u1", text="real message")])
    # A corrupt pre-existing output line must not abort the run.
    assert _convert_complete(input_file, output_file) > 0


def test_non_utf8_byte_in_input_does_not_abort(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    # Raw transcript streams can carry arbitrary bytes (e.g. tool output). A single
    # undecodable byte must not abort the (append-only) conversion pass.
    valid_line = json.dumps(make_user_record("u1", text="real message")).encode()
    input_file.write_bytes(b"\xff\xfe garbage byte line\n" + valid_line + b"\n")
    _convert_complete(input_file, output_file)
    assert [e["message"] for e in read_steps(output_file, "user")] == ["real message"]


def test_null_message_line_is_dropped(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    # A null (rather than dict) message carries no usable content, so the line is
    # dropped -- not raised on (AttributeError aborting the run) and not emitted as
    # an empty event. A following valid line must still convert.
    write_raw_transcript(
        input_file,
        [
            {"type": "assistant", "uuid": "u1", "timestamp": "2026-01-01T00:00:01Z", "message": None},
            {"type": "user", "uuid": "u2", "timestamp": "2026-01-01T00:00:02Z", "message": None},
            make_user_record("u3", text="real message"),
        ],
    )
    _convert_complete(input_file, output_file)
    assert [e["message"] for e in read_steps(output_file, "user")] == ["real message"]


def test_events_without_uuid_or_timestamp_are_skipped(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(input_file, [{"type": "user", "message": {"content": "no uuid"}}])
    assert _convert_complete(input_file, output_file) == 0


def test_missing_input_file_returns_zero(tmp_path: Path) -> None:
    assert _convert_complete(tmp_path / "missing.jsonl", tmp_path / "out.jsonl") == 0


def test_sorts_new_records_by_timestamp(tmp_path: Path) -> None:
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(
        input_file,
        [
            make_user_record("u1", text="later", timestamp="2026-01-01T00:00:09Z"),
            make_user_record("u2", text="earlier", timestamp="2026-01-01T00:00:01Z"),
        ],
    )
    _convert_complete(input_file, output_file)
    assert [e["message"] for e in read_steps(output_file, "user")] == ["earlier", "later"]


# -- The real session fixture --


def test_real_session_tool_result_converted_a_pass_after_its_call_keeps_tool_name(tmp_path: Path) -> None:
    """A tool_result arriving in a LATER pass than its (already-emitted) call must
    still resolve the tool name: the call-id->name map is built from the full input
    before the dedup skip, so it cannot be short-circuited by existing records.
    """
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    lines = _real_session_lines()
    split = next(i for i, line in enumerate(lines) if '"tool_result"' in line)

    # Pass 1: everything up to (excluding) the tool_results -- the tool_use
    # assistant messages are emitted now.
    input_file.write_text("\n".join(lines[:split]) + "\n")
    assert _convert_complete(input_file, output_file) > 0

    # Pass 2: the tool_results arrive; their calls are already in the output.
    input_file.write_text("\n".join(lines) + "\n")
    _convert_complete(input_file, output_file)

    assert [o["results"][0]["extra"]["tool_name"] for o in read_observations(output_file)] == ["Bash", "Bash"]


def test_real_session_fans_the_thinking_text_and_tool_lines_into_one_step(tmp_path: Path) -> None:
    """The fixture's first three assistant lines are one API response (one message.id)."""
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    input_file.write_text("\n".join(_real_session_lines()) + "\n")
    _convert_complete(input_file, output_file)
    steps = read_steps(output_file, "agent")
    # Two inferences: the fanned-out one and the second tool_use response.
    assert len(steps) == 2, steps
    assert steps[0]["message"].startswith("On it")
    assert [call["function_name"] for call in steps[0]["tool_calls"]] == ["Bash"]
    assert steps[1]["message"] == ""
    assert [call["function_name"] for call in steps[1]["tool_calls"]] == ["Bash"]


def test_real_session_slash_command_plumbing_is_filtered(tmp_path: Path) -> None:
    """Slash-command plumbing records (expansion tags, local stdout, isMeta caveat)
    are dropped entirely -- no fake user turns and no system steps."""
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    input_file.write_text("\n".join(_real_session_lines()) + "\n")
    _convert_complete(input_file, output_file)
    # The only user turn left is the genuine typed one.
    assert [e["message"] for e in read_steps(output_file, "user")] == [
        "put on a little tool call show for me how are we"
    ]
    assert read_steps(output_file, "system") == []


def test_real_session_genuine_turns_all_present_and_schema_valid(tmp_path: Path) -> None:
    """Filtering must not over-filter: every genuine turn converts exactly once,
    including a tool_result whose output merely QUOTES command markup mid-text,
    and every emitted record passes the canonical schema."""
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    input_file.write_text("\n".join(_real_session_lines()) + "\n")
    _convert_complete(input_file, output_file)
    events = read_stream(output_file)
    # 1 user turn + 2 inferences + their 2 tool results, behind the stream header.
    assert [(e["type"], e.get("source")) for e in events] == [
        ("header", None),
        ("step", "user"),
        ("step", "agent"),
        ("observation", None),
        ("step", "agent"),
        ("observation", None),
    ]
    assert len({e["event_id"] for e in events}) == len(events)
    for event in events:
        assert validate_common_transcript_record(event) is None, event
    markup_quoting = [o for o in read_observations(output_file) if "<command-name>" in o["results"][0]["content"]]
    assert len(markup_quoting) == 1
    assert markup_quoting[0]["results"][0]["extra"]["tool_name"] == "Bash"


def test_user_text_quoting_command_markup_mid_text_stays_user(tmp_path: Path) -> None:
    """The plumbing filter anchors on the leading tag: a human message that merely
    mentions the markup mid-text must still appear as a user turn."""
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    write_raw_transcript(input_file, [make_user_record("u1", text="what does the <command-name> markup mean?")])
    _convert_complete(input_file, output_file)
    assert len(read_steps(output_file, "user")) == 1
