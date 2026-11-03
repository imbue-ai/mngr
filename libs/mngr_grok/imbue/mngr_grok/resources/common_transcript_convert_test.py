import json
import logging
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record
from imbue.mngr_grok.resources import common_transcript_convert as convert_module
from imbue.mngr_grok.resources.testing import SESSION_FIXTURES_DIR
from imbue.mngr_grok.resources.testing import render_jsonl
from imbue.mngr_grok.resources.testing import tag_native_lines


def _raw_entries(scenario: str) -> list[dict[str, Any]]:
    """The raw-transcript entries the streamer writes for a fixture: roots first, then children of the first root."""
    scenario_dir = SESSION_FIXTURES_DIR / scenario
    root_ids = (scenario_dir / "roots").read_text().split()
    child_ids = sorted(path.name for path in scenario_dir.iterdir() if path.is_dir() and path.name not in root_ids)
    return [
        entry
        for session_id in root_ids + child_ids
        for entry in tag_native_lines(
            session_id,
            None if session_id in root_ids else root_ids[0],
            (scenario_dir / session_id / "updates.jsonl").read_bytes().splitlines(keepends=True),
        )
    ]


def _raw_entries_before_first_turn_end(scenario: str) -> list[dict[str, Any]]:
    entries = _raw_entries(scenario)
    turn_end_idx = next(
        idx
        for idx, entry in enumerate(entries)
        if entry["record"]["params"]["update"]["sessionUpdate"] == "turn_completed"
    )
    return entries[:turn_end_idx]


def _summarise(record: dict[str, Any]) -> tuple[Any, ...]:
    if record["type"] == "observation":
        result = record["results"][0]
        return ("observation", result["source_call_id"], result["extra"]["is_error"])
    extra = record["extra"]
    return (
        record["source"],
        record["message"],
        extra.get("finish_reason"),
        tuple(tool_call["function_name"] for tool_call in record.get("tool_calls", ())),
        extra.get("is_sidechain", False),
    )


def _records(scenario: str) -> list[dict[str, Any]]:
    return convert_module.build_common_records(_raw_entries(scenario), is_emitting_open_steps=True)


def test_plain_turn_becomes_a_user_step_and_a_decorated_agent_step() -> None:
    user_step, agent_step = _records("plain")

    assert user_step == {
        "type": "step",
        "event_id": "grok-01a156d6-f927-7091-91a4-f6692a5800da-839-user",
        "emitter": "grok/common_transcript",
        "timestamp": "2026-10-20T01:04:53.416Z",
        "source": "user",
        "message": "hello plain",
        "extra": {"session_id": "01a156d6-f927-7091-91a4-f6692a5800da"},
    }
    assert agent_step["message"].startswith("STUB_REPLY: hello from the stub model")
    assert agent_step["model_name"] == "stub-model"
    assert agent_step["metrics"] == {
        "prompt_tokens": 4,
        "completion_tokens": 2,
        "cached_tokens": 0,
        "extra": {"total_tokens": 6, "reasoning_tokens": 0, "cache_creation_tokens": 0, "model_calls": 1},
    }
    assert agent_step["extra"] == {
        "session_id": "01a156d6-f927-7091-91a4-f6692a5800da",
        "finish_reason": "end_turn",
        "prompt_id": "92ca53e0-b715-48bc-aa56-e448fdb65998",
    }


def test_tool_call_is_paired_with_its_result_and_the_next_inference_is_its_own_step() -> None:
    records = _records("tool")

    assert [_summarise(record) for record in records] == [
        ("user", "TOOLTEST please", None, (), False),
        ("agent", "", None, ("run_terminal_command",), False),
        ("observation", "call_stub_2", False),
        ("agent", "STUB_REPLY: tool finished", "end_turn", (), False),
    ]
    assert records[1]["tool_calls"][0]["arguments"] == {
        "command": "echo stub-tool-ran > tool_ran.txt && echo done",
        "description": "write marker file",
    }
    assert records[2]["results"][0]["content"] == "done\n"
    assert records[2]["results"][0]["extra"]["tool_name"] == "run_terminal_command"


def test_interrupted_turns_keep_their_partial_reply_and_cancelled_reason() -> None:
    assert [_summarise(record) for record in _records("interrupt")] == [
        ("user", "SLOWTEST interrupt me while streaming", None, (), False),
        ("agent", "STUB_REPLY: slow one two", "cancelled", (), False),
        ("user", "SLOWTOOL interrupt me during the tool", None, (), False),
        ("agent", "", "cancelled", ("run_terminal_command",), False),
        ("user", "hello after interrupts", None, (), False),
        (
            "agent",
            "STUB_REPLY: hello from the stub model k=4 nuser=5 last=[user_query hello after interrupts /user_query]",
            "end_turn",
            (),
            False,
        ),
    ]


def test_send_now_cut_turn_and_the_queued_message_are_separate_turns() -> None:
    summaries = [_summarise(record) for record in _records("send_now")]

    assert summaries[:3] == [
        ("user", "SLOWTEST first long turn", None, (), False),
        ("agent", "STUB_REPLY: slow one two three", "cancelled", (), False),
        ("user", "queued follow up", None, (), False),
    ]
    assert summaries[3][2] == "end_turn"


def test_a_blocked_stop_continuation_is_a_second_step_that_carries_the_turn_end() -> None:
    first_turn = [_summarise(record) for record in _records("stop_block")][:3]

    assert first_turn == [
        ("user", "hello block one", None, (), False),
        (
            "agent",
            "STUB_REPLY: hello from the stub model k=2 nuser=3 last=[user_query hello block one /user_query]",
            None,
            (),
            False,
        ),
        (
            "agent",
            "STUB_REPLY: hello from the stub model k=3 nuser=4 last=[k: - STOPGATE_FEEDBACK please continue once more]",
            "end_turn",
            (),
            False,
        ),
    ]


def test_wake_prompts_are_skipped_but_the_wake_reply_is_kept() -> None:
    summaries = [_summarise(record) for record in _records("background_shell")]

    assert [summary[0] for summary in summaries] == ["user", "agent", "observation", "agent", "agent"]
    assert summaries[-1][1] == "STUB_WAKE_REPLY: acknowledged background completion k=5"
    assert summaries[-1][2] == "end_turn"


def test_a_failed_turn_without_output_still_records_its_end() -> None:
    failed_step = _records("failed_wake")[-1]

    assert failed_step["source"] == "agent"
    assert failed_step["message"] == ""
    assert failed_step["extra"]["finish_reason"] == "error"
    assert failed_step["extra"]["agent_result"] == "empty response from model (no_visible_content)"
    assert failed_step["extra"]["prompt_id"].startswith("task-completed-")
    assert "metrics" not in failed_step


def test_child_sessions_fold_into_the_stream_as_sidechains_linked_from_the_spawning_call() -> None:
    records = _records("background_subagent")
    spawn_step = records[1]
    spawn_result = records[2]
    child_records = [record for record in records if record.get("extra", {}).get("is_sidechain")]

    link = {"subagent_session_id": "01a156db-10ae-70c2-9a0f-a0dff6d5f376", "subagent_type": "general-purpose"}
    assert spawn_step["tool_calls"][0]["extra"] == link
    assert {key: spawn_result["results"][0]["extra"][key] for key in link} == link
    assert [_summarise(record) for record in child_records] == [
        ("user", "say hi from the subagent", None, (), True),
        (
            "agent",
            "STUB_CHILD: slow child one two three four five six seven eight nine ten eleven twelve",
            "end_turn",
            (),
            True,
        ),
    ]
    assert child_records[1]["extra"]["parent_session_id"] == "01a156db-07ea-7993-b2e6-dcaa7cb0a81c"
    assert child_records[1]["metrics"]["prompt_tokens"] == 32


def test_two_roots_after_new_are_two_lanes_of_one_stream() -> None:
    sessions = [record["extra"]["session_id"] for record in _records("new_session_and_resume")]

    assert sessions == ["f603168f-7fec-4427-9b3f-f621a162e742"] * 4 + ["01a156dc-9ff8-7e32-a636-a71e85c2dfee"] * 2


def test_record_ids_come_from_session_and_offset_so_repeated_event_ids_stay_distinct() -> None:
    entries = _raw_entries("new_session_and_resume")
    for entry in entries:
        entry["record"]["params"]["_meta"]["eventId"] = "f603168f-7fec-4427-9b3f-f621a162e742-1"

    event_ids = [record["event_id"] for record in convert_module.build_common_records(entries, True)]

    assert len(event_ids) == 6
    assert len(set(event_ids)) == 6


def test_an_open_step_is_held_until_its_closing_record_unless_emitting_open_steps() -> None:
    before_turn_end = _raw_entries_before_first_turn_end("plain")

    held = convert_module.build_common_records(before_turn_end, is_emitting_open_steps=False)
    emitted = convert_module.build_common_records(before_turn_end, is_emitting_open_steps=True)

    assert [record["source"] for record in held] == ["user"]
    assert [record["source"] for record in emitted] == ["user", "agent"]
    assert "finish_reason" not in emitted[1]["extra"]


def test_the_session_end_closes_a_step_left_open_by_a_quit() -> None:
    entries = _raw_entries("plain")
    without_turn_end = [
        entry for entry in entries if entry["record"]["params"]["update"]["sessionUpdate"] != "turn_completed"
    ]

    records = convert_module.build_common_records(without_turn_end, is_emitting_open_steps=False)

    assert [record["source"] for record in records] == ["user", "agent"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "The user sent a message while you were working:\n<user_query>\nqueued follow up\n</user_query>\n"
            "If the user is asking for a response, address the user first.",
            "queued follow up",
        ),
        ("plain words", "plain words"),
    ],
)
def test_send_now_wrapper_is_unwrapped(text: str, expected: str) -> None:
    assert convert_module.unwrap_user_text(text) == expected


def test_reasoning_chunks_become_reasoning_content() -> None:
    entries = _raw_entries("plain")
    thought = json.loads(json.dumps(entries[3]))
    thought["record"]["params"]["update"]["sessionUpdate"] = "agent_thought_chunk"
    thought["record"]["params"]["update"]["content"]["text"] = "thinking first"
    thought["offset"] = entries[3]["offset"] - 1

    records = convert_module.build_common_records(entries[:3] + [thought] + entries[3:], is_emitting_open_steps=True)

    assert records[1]["reasoning_content"] == "thinking first"
    assert records[1]["message"].startswith("STUB_REPLY")


def test_unknown_kinds_and_malformed_entries_are_ignored() -> None:
    entries: list[dict[str, Any]] = [
        {"session_id": "s1", "offset": 0, "record": {"params": {"update": {"sessionUpdate": "plan", "entries": []}}}},
        {"session_id": "s1", "offset": "not an int", "record": {}},
        {"session_id": None, "offset": 1, "record": {}},
        {"session_id": "s1", "offset": 2, "unparsed": "{broken"},
        {"session_id": "s1", "offset": 3, "record": {"params": "not a mapping"}},
        {"session_id": "s1", "offset": 4, "record": {"params": {"update": {"sessionUpdate": "tool_call_update"}}}},
    ]

    assert convert_module.build_common_records(entries, is_emitting_open_steps=True) == []


def test_convert_appends_only_records_not_already_in_the_output(tmp_path: Path) -> None:
    input_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "out" / "events.jsonl"
    entries = _raw_entries("tool")
    input_path.write_text(render_jsonl(entries[:8]) + "{torn\n")

    first_count = convert_module.convert(str(input_path), str(output_path), False, "agent-1")
    input_path.write_text(render_jsonl(entries))
    second_count = convert_module.convert(str(input_path), str(output_path), False, "agent-1")
    third_count = convert_module.convert(str(input_path), str(output_path), True, "agent-1")

    records = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert (first_count, second_count, third_count) == (4, 1, 0)
    assert records[0] == convert_module.build_header_record("agent-1")
    assert [record["type"] for record in records] == ["header", "step", "step", "observation", "step"]
    assert len({record["event_id"] for record in records}) == len(records)


def test_convert_of_a_missing_input_appends_nothing(tmp_path: Path) -> None:
    assert convert_module.convert(str(tmp_path / "missing.jsonl"), str(tmp_path / "out.jsonl"), True, "a") == 0
    assert not (tmp_path / "out.jsonl").exists()


def test_header_id_is_distinct_per_agent() -> None:
    first = convert_module.build_header_record("agent-a")
    second = convert_module.build_header_record("agent-b")

    assert first["event_id"] != second["event_id"]
    assert validate_common_transcript_record(first) is None


def test_metrics_skip_missing_and_boolean_counts() -> None:
    assert convert_module.build_metrics({"inputTokens": True, "outputTokens": 3, "usageIsIncomplete": True}) == {
        "completion_tokens": 3,
        "extra": {"usage_is_incomplete": True},
    }


@pytest.mark.parametrize(
    ("raw_input", "expected"),
    [({"a": 1}, {"a": 1}), (None, {}), ("ls -la", {"_raw": "ls -la"}), ([1, 2], {"_raw": "[1,2]"})],
)
def test_tool_arguments_always_form_an_object(raw_input: object, expected: dict[str, Any]) -> None:
    assert convert_module.build_tool_arguments(raw_input) == expected


def _entry(offset: int, update: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": "s1",
        "parent_session_id": None,
        "offset": offset,
        "record": {"params": {"update": update, "_meta": {"agentTimestampMs": 1792458294289}}},
    }


def test_a_repeated_tool_call_record_adds_the_call_once_and_a_result_without_text_has_no_content() -> None:
    entries = [
        _entry(0, {"sessionUpdate": "tool_call", "toolCallId": "call_a", "title": "first title", "rawInput": None}),
        _entry(1, {"sessionUpdate": "tool_call", "toolCallId": "call_a", "title": "second title"}),
        _entry(2, {"sessionUpdate": "tool_call_update", "toolCallId": "call_a", "status": "failed", "content": None}),
    ]

    agent_step, observation = convert_module.build_common_records(entries, is_emitting_open_steps=True)

    assert agent_step["tool_calls"] == [{"tool_call_id": "call_a", "function_name": "first title", "arguments": {}}]
    assert observation["results"] == [
        {
            "source_call_id": "call_a",
            "extra": {"session_id": "s1", "is_error": True, "tool_name": "first title"},
        }
    ]


@pytest.mark.parametrize("status", [{"state": "completed"}, ["completed"]])
def test_a_tool_call_update_whose_status_is_not_a_string_is_ignored(status: object) -> None:
    entries = [
        _entry(0, {"sessionUpdate": "tool_call", "toolCallId": "call_a", "title": "run"}),
        _entry(1, {"sessionUpdate": "tool_call_update", "toolCallId": "call_a", "status": status}),
        _entry(2, {"sessionUpdate": "tool_call_update", "toolCallId": "call_a", "status": "completed"}),
    ]

    agent_step, observation = convert_module.build_common_records(entries, is_emitting_open_steps=True)

    assert [tool_call["tool_call_id"] for tool_call in agent_step["tool_calls"]] == ["call_a"]
    assert observation["event_id"] == "grok-s1-2-observation"
    assert observation["results"][0]["extra"]["is_error"] is False


def test_empty_user_chunks_add_no_text_and_open_no_step() -> None:
    entries = [
        _entry(0, {"sessionUpdate": "user_message_chunk", "content": {"text": ""}}),
        _entry(1, {"sessionUpdate": "user_message_chunk", "content": {"text": "hello"}}),
        _entry(2, {"sessionUpdate": "user_message_chunk", "content": {}}),
    ]

    records = convert_module.build_common_records(entries, is_emitting_open_steps=True)

    assert [(record["event_id"], record["message"]) for record in records] == [("grok-s1-1-user", "hello")]


_SPAWN_SUBAGENT_CALL: Final[dict[str, Any]] = {
    "sessionUpdate": "tool_call",
    "toolCallId": "call_spawn",
    "_meta": {"x.ai/tool": {"name": "spawn_subagent"}},
    "rawInput": {"description": "look around"},
}


@pytest.mark.parametrize(
    ("tool_call", "spawned"),
    [
        pytest.param(
            _SPAWN_SUBAGENT_CALL,
            {"sessionUpdate": "subagent_spawned", "description": "look around"},
            id="spawn_without_a_child_id",
        ),
        pytest.param(
            {**_SPAWN_SUBAGENT_CALL, "_meta": {"x.ai/tool": {"name": "run_terminal_command"}}},
            {"sessionUpdate": "subagent_spawned", "child_session_id": "child-1", "description": "look around"},
            id="no_spawning_call",
        ),
    ],
)
def test_a_subagent_spawn_without_a_child_or_a_spawning_call_links_nothing(
    tool_call: dict[str, Any], spawned: dict[str, Any]
) -> None:
    records = convert_module.build_common_records([_entry(0, tool_call), _entry(1, spawned)], True)

    assert [record["type"] for record in records] == ["step"]
    assert "extra" not in records[0]["tool_calls"][0]


def test_a_subagent_spawn_links_the_open_spawn_call_with_its_description() -> None:
    entries = [
        _entry(0, {**_SPAWN_SUBAGENT_CALL, "toolCallId": "call_first", "rawInput": {"description": "first look"}}),
        _entry(1, {**_SPAWN_SUBAGENT_CALL, "toolCallId": "call_second", "rawInput": {"description": "second look"}}),
        _entry(2, {"sessionUpdate": "subagent_spawned", "child_session_id": "child-2", "description": "second look"}),
    ]

    (agent_step,) = convert_module.build_common_records(entries, is_emitting_open_steps=True)

    first_call, second_call = agent_step["tool_calls"]
    assert "extra" not in first_call
    assert second_call["extra"] == {"subagent_session_id": "child-2"}


@pytest.mark.parametrize(
    ("model_usage", "expected_model_name"),
    [
        pytest.param({"grok-build": {"inputTokens": 3}}, "grok-build", id="one_model"),
        pytest.param({"grok-build": {"inputTokens": 3}, "grok-4.6": {"inputTokens": 1}}, None, id="two_models"),
    ],
)
def test_a_turn_end_names_the_model_only_when_its_usage_names_exactly_one(
    model_usage: dict[str, Any], expected_model_name: str | None
) -> None:
    entries = [
        _entry(0, {"sessionUpdate": "agent_message_chunk", "content": {"text": "done"}}),
        _entry(
            1, {"sessionUpdate": "turn_completed", "stop_reason": "end_turn", "usage": {"modelUsage": model_usage}}
        ),
    ]

    (agent_step,) = convert_module.build_common_records(entries, is_emitting_open_steps=False)

    assert agent_step.get("model_name") == expected_model_name


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        pytest.param({"params": {"_meta": {"agentTimestampMs": 1792458294289}}}, "2026-10-20T01:04:54.289Z", id="ms"),
        pytest.param(
            {"params": {"_meta": {"agentTimestampMs": True}}, "timestamp": 1792458300},
            "2026-10-20T01:05:00.000Z",
            id="seconds",
        ),
    ],
)
def test_record_timestamp_prefers_agent_milliseconds_then_record_seconds(
    record: dict[str, Any], expected: str
) -> None:
    assert convert_module.record_timestamp(record) == expected


def test_record_timestamp_without_a_time_is_now() -> None:
    before = time.time()

    timestamp = convert_module.record_timestamp({"params": "not a mapping"})

    parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
    assert before - 0.001 <= parsed <= time.time()


def test_blank_lines_and_torn_output_lines_are_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    input_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "out" / "events.jsonl"
    input_path.write_text("".join(json.dumps(entry) + "\n\n" for entry in _raw_entries("plain")) + "   \n")
    first_count = convert_module.convert(str(input_path), str(output_path), True, "agent-1")
    with output_path.open("a") as output_file:
        output_file.write('\n{"event_id": "torn\n')

    with caplog.at_level(logging.WARNING, logger="grok_common_transcript_convert"):
        second_count = convert_module.convert(str(input_path), str(output_path), True, "agent-1")

    assert (first_count, second_count) == (3, 0)
    assert [record.getMessage().split(":")[0] for record in caplog.records] == [
        "Ignored unparseable common transcript line 5"
    ]


def test_records_appended_after_a_torn_last_output_line_start_on_their_own_line(tmp_path: Path) -> None:
    input_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "out" / "events.jsonl"
    input_path.write_text(render_jsonl(_raw_entries("plain")))
    output_path.parent.mkdir()
    output_path.write_text('{"event_id": "torn-by-a-kill')

    appended_count = convert_module.convert(str(input_path), str(output_path), True, "agent-1")

    torn_line, *appended_lines = output_path.read_text().splitlines()
    assert torn_line == '{"event_id": "torn-by-a-kill'
    assert [json.loads(line)["type"] for line in appended_lines] == ["header", "step", "step"]
    assert appended_count == len(appended_lines)


@pytest.mark.parametrize(
    ("state_dir_suffix", "emit_open_steps", "expected_agent_id", "expected_sources"),
    [
        pytest.param("agents/agent-0a1b/", "1", "agent-0a1b", [None, "user", "agent"], id="emitting_open_steps"),
        pytest.param("agents/agent-0a1b", "", "agent-0a1b", [None, "user"], id="holding_open_steps"),
        pytest.param(None, "1", "", [None, "user", "agent"], id="no_state_dir"),
    ],
)
def test_convert_from_environment_reads_the_files_flag_and_agent_id(
    tmp_path: Path,
    state_dir_suffix: str | None,
    emit_open_steps: str,
    expected_agent_id: str,
    expected_sources: list[str | None],
) -> None:
    input_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "out" / "events.jsonl"
    before_turn_end = _raw_entries_before_first_turn_end("plain")
    input_path.write_text(render_jsonl(before_turn_end))
    environ = {
        "_INPUT_FILE": str(input_path),
        "_OUTPUT_FILE": str(output_path),
        "_MNGR_EMIT_OPEN_STEPS": emit_open_steps,
    }
    if state_dir_suffix is not None:
        environ["MNGR_AGENT_STATE_DIR"] = str(tmp_path) + "/" + state_dir_suffix

    count = convert_module.convert_from_environment(environ)

    records = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert count == len(expected_sources)
    assert records[0] == convert_module.build_header_record(expected_agent_id)
    assert [record.get("source") for record in records] == expected_sources
