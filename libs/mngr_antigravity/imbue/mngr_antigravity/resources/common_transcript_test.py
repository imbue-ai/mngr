"""Tests for the antigravity common_transcript.sh converter.

Exercises the script's core behaviors by running it with --single-pass in a
controlled filesystem layout. The converter reads its input from
``$MNGR_AGENT_STATE_DIR/logs/antigravity_transcript/events.jsonl`` (the raw
transcript produced by ``stream_transcript.sh``), so tests seed that file
directly rather than running agy. Each event in the seeded raw transcript
must carry the ``_mngr_conv_id`` field that the streamer adds; this is the
key the converter uses to scope tool-call/result pairing within a single
conversation.

Each test sets up:
  - A fake agent state dir at tmp_path/agent
  - A stub mngr_log.sh in commands/
  - A seeded raw transcript at logs/antigravity_transcript/events.jsonl
  - Runs the converter once via --single-pass and inspects the output
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record

_SCRIPT_PATH = Path(__file__).parent / "common_transcript.sh"


def _make_event(
    *,
    conv_id: str,
    step_index: int,
    source: str,
    type_: str,
    timestamp: str = "2026-05-21T07:00:00Z",
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    status: str = "DONE",
) -> str:
    """Build one raw-transcript line, including the ``_mngr_conv_id`` the streamer adds."""
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
    return json.dumps(body)


def _user_input(conv_id: str, step_index: int, prompt_text: str) -> str:
    """USER_EXPLICIT/USER_INPUT carrying the clean typed text agy's SQLite store records."""
    return _make_event(
        conv_id=conv_id,
        step_index=step_index,
        source="USER_EXPLICIT",
        type_="USER_INPUT",
        content=prompt_text,
    )


def _planner_response(
    conv_id: str,
    step_index: int,
    text: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
) -> str:
    return _make_event(
        conv_id=conv_id,
        step_index=step_index,
        source="MODEL",
        type_="PLANNER_RESPONSE",
        content=text,
        tool_calls=tool_calls,
    )


def _code_action(conv_id: str, step_index: int, content: str, status: str = "DONE") -> str:
    return _make_event(
        conv_id=conv_id,
        step_index=step_index,
        source="MODEL",
        type_="CODE_ACTION",
        content=content,
        status=status,
    )


def _conversation_history(conv_id: str, step_index: int) -> str:
    """SYSTEM/CONVERSATION_HISTORY bookkeeping event that must be dropped by the converter."""
    return _make_event(
        conv_id=conv_id,
        step_index=step_index,
        source="SYSTEM",
        type_="CONVERSATION_HISTORY",
    )


@pytest.fixture
def state_dir(tmp_path: Path, stub_mngr_log_sh: str) -> Path:
    """Per-test fake $MNGR_AGENT_STATE_DIR with stub mngr_log.sh installed."""
    state = tmp_path / "agent"
    (state / "commands").mkdir(parents=True)
    (state / "logs" / "antigravity_transcript").mkdir(parents=True)
    (state / "commands" / "mngr_log.sh").write_text(stub_mngr_log_sh)
    return state


def _write_raw_transcript(state_dir: Path, lines: list[str]) -> None:
    raw_path = state_dir / "logs" / "antigravity_transcript" / "events.jsonl"
    raw_path.write_text("\n".join(lines) + "\n")


def _run_converter(state_dir: Path) -> str:
    """Run common_transcript.sh in single-pass mode against the seeded raw transcript.

    Returns the converter's combined stderr (from both the bash main script
    and the heredoc Python) so callers can assert on loud-error messages
    emitted by the conversion pass. The shared logging library also writes
    structured warnings to events/logs/common_transcript/events.jsonl --
    stderr is the easier surface to inspect in tests.
    """
    env = {**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)}
    result = subprocess.run(
        ["bash", str(_SCRIPT_PATH), "--single-pass"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    # A Python traceback in the converter would mean the conversion pass
    # crashed mid-loop and dropped subsequent events. Surface it in the
    # failure message rather than letting a downstream assertion fail
    # mysteriously.
    assert "Traceback" not in result.stderr, result.stderr
    return result.stderr


def _read_common_events(state_dir: Path) -> list[dict[str, Any]]:
    output_path = state_dir / "events" / "antigravity" / "common_transcript" / "events.jsonl"
    if not output_path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in output_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        events.append(json.loads(line))
    return events


# -- Tests --


def test_user_input_is_converted_to_user_message(state_dir: Path) -> None:
    """USER_EXPLICIT/USER_INPUT -> user_message carrying agy's clean typed text.

    agy's SQLite store (via decode_agy_transcript.py) records the bare typed text in
    ``CortexStepUserInput.query``; the converter passes it through, stripped of surrounding
    whitespace, with no envelope handling.
    """
    _write_raw_transcript(state_dir, [_user_input("conv-A", 0, "What is 2+2?")])

    _run_converter(state_dir)

    events = _read_common_events(state_dir)
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "user_message"
    assert event["role"] == "user"
    assert event["content"] == "What is 2+2?"
    assert event["conversation_id"] == "conv-A"
    assert event["step_index"] == 0
    assert event["source"] == "antigravity/common_transcript"


def test_planner_response_without_tool_calls_is_assistant_message(state_dir: Path) -> None:
    _write_raw_transcript(
        state_dir,
        [
            _user_input("conv-A", 0, "hi"),
            _planner_response("conv-A", 2, text="Hello back."),
        ],
    )

    _run_converter(state_dir)

    events = _read_common_events(state_dir)
    assert [e["type"] for e in events] == ["user_message", "assistant_message"]
    assistant = events[1]
    assert assistant["role"] == "assistant"
    assert assistant["text"] == "Hello back."
    assert assistant["tool_calls"] == []
    assert assistant["conversation_id"] == "conv-A"


def test_planner_response_with_tool_calls_emits_synthetic_tool_call_ids(state_dir: Path) -> None:
    """agy's transcript doesn't include tool_call_ids; the converter must mint stable ones."""
    _write_raw_transcript(
        state_dir,
        [
            _user_input("conv-A", 0, "write a file"),
            _planner_response(
                "conv-A",
                2,
                tool_calls=[
                    {"name": "write_to_file", "args": {"path": "/tmp/x", "content": "hi"}},
                ],
            ),
        ],
    )

    _run_converter(state_dir)

    events = _read_common_events(state_dir)
    assistant = events[1]
    assert len(assistant["tool_calls"]) == 1
    tc = assistant["tool_calls"][0]
    assert tc["tool_call_id"] == "conv-A-2-tc0"
    assert tc["tool_name"] == "write_to_file"
    # Args are JSON-serialized into the preview
    assert "/tmp/x" in tc["input_preview"]


def test_code_action_pairs_with_preceding_planner_response_tool_call(state_dir: Path) -> None:
    """MODEL/CODE_ACTION -> tool_result whose tool_call_id matches the last assistant tool call in the same conversation."""
    _write_raw_transcript(
        state_dir,
        [
            _user_input("conv-A", 0, "create test.txt"),
            _planner_response(
                "conv-A",
                2,
                tool_calls=[{"name": "write_to_file", "args": {"path": "test.txt"}}],
            ),
            _code_action("conv-A", 3, content="Created test.txt"),
        ],
    )

    _run_converter(state_dir)

    events = _read_common_events(state_dir)
    types = [e["type"] for e in events]
    assert types == ["user_message", "assistant_message", "tool_result"]
    tool_result = events[2]
    assert tool_result["tool_call_id"] == "conv-A-2-tc0"
    assert tool_result["tool_name"] == "write_to_file"
    assert tool_result["output"] == "Created test.txt"
    assert tool_result["is_error"] is False


def test_planner_response_with_multiple_tool_calls_pairs_last_with_code_action(state_dir: Path) -> None:
    """A multi-tool-call PLANNER_RESPONSE lists every tool_call but only the last one
    gets paired with the subsequent CODE_ACTION.

    This documents the converter's current behavior: agy emits one CODE_ACTION
    per planner response (per the script's top-level docstring) regardless of
    how many tool_calls the response contained, so only the last tool_call has
    a matching tool_result event. The earlier tool_calls still appear in the
    assistant_message.tool_calls array. If agy's emit pattern ever changes to
    one CODE_ACTION per tool_call, this test (and the pairing logic) will need
    to be revisited.
    """
    _write_raw_transcript(
        state_dir,
        [
            _user_input("conv-A", 0, "do two things"),
            _planner_response(
                "conv-A",
                2,
                tool_calls=[
                    {"name": "first_tool", "args": {"a": 1}},
                    {"name": "second_tool", "args": {"b": 2}},
                ],
            ),
            _code_action("conv-A", 3, content="paired output"),
        ],
    )

    _run_converter(state_dir)

    events = _read_common_events(state_dir)
    assistant = next(e for e in events if e["type"] == "assistant_message")
    assert [tc["tool_name"] for tc in assistant["tool_calls"]] == ["first_tool", "second_tool"]
    assert [tc["tool_call_id"] for tc in assistant["tool_calls"]] == ["conv-A-2-tc0", "conv-A-2-tc1"]

    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_results) == 1
    # The CODE_ACTION pairs with the LAST tool_call (tc1), not the first.
    assert tool_results[0]["tool_call_id"] == "conv-A-2-tc1"
    assert tool_results[0]["tool_name"] == "second_tool"


def test_code_action_with_failed_status_marks_is_error(state_dir: Path) -> None:
    _write_raw_transcript(
        state_dir,
        [
            _planner_response(
                "conv-A",
                2,
                tool_calls=[{"name": "write_to_file", "args": {}}],
            ),
            _code_action("conv-A", 3, content="permission denied", status="ERROR"),
        ],
    )

    _run_converter(state_dir)

    events = _read_common_events(state_dir)
    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert tool_result["is_error"] is True


def test_code_action_without_preceding_tool_call_is_dropped(state_dir: Path) -> None:
    """A bare CODE_ACTION (no PLANNER_RESPONSE tool_calls earlier) has nothing to pair with."""
    _write_raw_transcript(
        state_dir,
        [
            _code_action("conv-A", 0, content="orphan"),
        ],
    )

    _run_converter(state_dir)

    assert _read_common_events(state_dir) == []


def test_conversation_history_is_dropped(state_dir: Path) -> None:
    """SYSTEM/CONVERSATION_HISTORY is bookkeeping and must not appear in the common output."""
    _write_raw_transcript(
        state_dir,
        [
            _user_input("conv-A", 0, "hi"),
            _conversation_history("conv-A", 1),
            _planner_response("conv-A", 2, text="hello"),
        ],
    )

    _run_converter(state_dir)

    events = _read_common_events(state_dir)
    types = [e["type"] for e in events]
    assert types == ["user_message", "assistant_message"]


def test_unknown_source_type_combination_is_dropped(state_dir: Path) -> None:
    """Forward-compat: any event the converter doesn't recognize is silently skipped, not crashed on."""
    _write_raw_transcript(
        state_dir,
        [
            _user_input("conv-A", 0, "hi"),
            _make_event(
                conv_id="conv-A",
                step_index=1,
                source="MODEL",
                type_="SOMETHING_NEW_FROM_FUTURE_AGY",
                content="x",
            ),
            _planner_response("conv-A", 2, text="ok"),
        ],
    )

    _run_converter(state_dir)

    events = _read_common_events(state_dir)
    types = [e["type"] for e in events]
    assert types == ["user_message", "assistant_message"]


def test_tool_call_pairing_is_scoped_per_conversation(state_dir: Path) -> None:
    """Conv A's CODE_ACTION must not pair with Conv B's tool call (or vice versa)."""
    _write_raw_transcript(
        state_dir,
        [
            _planner_response(
                "conv-A",
                2,
                tool_calls=[{"name": "tool_a", "args": {}}],
            ),
            _planner_response(
                "conv-B",
                2,
                tool_calls=[{"name": "tool_b", "args": {}}],
            ),
            # Pair conv-A's CODE_ACTION; if conv-A's pending call leaked into conv-B
            # bucket the pairing would be wrong.
            _code_action("conv-A", 3, content="result_a"),
            _code_action("conv-B", 3, content="result_b"),
        ],
    )

    _run_converter(state_dir)

    events = _read_common_events(state_dir)
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert {tr["tool_name"]: tr["output"] for tr in tool_results} == {
        "tool_a": "result_a",
        "tool_b": "result_b",
    }


def test_converter_is_idempotent_across_runs(state_dir: Path) -> None:
    """Re-running the converter must not duplicate events.

    Event ids are derived deterministically from (conv_id, step_index, kind);
    the converter dedupes against the existing output file on each pass.
    """
    raw = [
        _user_input("conv-A", 0, "hi"),
        _planner_response("conv-A", 2, text="hello"),
    ]
    _write_raw_transcript(state_dir, raw)

    _run_converter(state_dir)
    first = _read_common_events(state_dir)
    _run_converter(state_dir)
    second = _read_common_events(state_dir)

    assert first == second
    assert len(second) == 2


def test_converter_appends_only_new_events_on_incremental_runs(state_dir: Path) -> None:
    """A second pass with extra raw events appends only the new ones."""
    _write_raw_transcript(state_dir, [_user_input("conv-A", 0, "first")])
    _run_converter(state_dir)
    first_pass = _read_common_events(state_dir)
    assert len(first_pass) == 1

    _write_raw_transcript(
        state_dir,
        [
            _user_input("conv-A", 0, "first"),
            _planner_response("conv-A", 2, text="second"),
        ],
    )
    _run_converter(state_dir)
    second_pass = _read_common_events(state_dir)
    assert [e["type"] for e in second_pass] == ["user_message", "assistant_message"]


def test_events_without_mngr_conv_id_are_dropped(state_dir: Path) -> None:
    """The streamer always injects _mngr_conv_id; without it the converter can't correlate."""
    raw = json.dumps(
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "created_at": "2026-05-21T07:00:00Z",
            "content": "hi",
        }
    )
    _write_raw_transcript(state_dir, [raw])

    _run_converter(state_dir)

    assert _read_common_events(state_dir) == []


def test_malformed_lines_are_skipped_not_fatal(state_dir: Path) -> None:
    """A partial / truncated JSON line shouldn't abort the rest of the conversion."""
    raw_path = state_dir / "logs" / "antigravity_transcript" / "events.jsonl"
    raw_path.write_text("{ not valid json\n" + _user_input("conv-A", 0, "after the broken line") + "\n")

    _run_converter(state_dir)

    events = _read_common_events(state_dir)
    assert len(events) == 1
    assert events[0]["content"] == "after the broken line"


def test_event_ids_are_stable_and_per_conversation(state_dir: Path) -> None:
    """Two conversations with the same step_index produce distinct event ids."""
    _write_raw_transcript(
        state_dir,
        [
            _user_input("conv-A", 0, "a-message"),
            _user_input("conv-B", 0, "b-message"),
        ],
    )

    _run_converter(state_dir)

    events = _read_common_events(state_dir)
    ids = sorted(e["event_id"] for e in events)
    assert ids == ["conv-A-0-user", "conv-B-0-user"]


def test_emitted_common_records_conform_to_canonical_schema(state_dir: Path) -> None:
    """Every record antigravity's converter emits must validate against the shared envelope schema.

    Guards against the antigravity emitter (common_transcript.sh) and the canonical schema
    (imbue.mngr.agents.common_transcript_records) drifting apart. Drives all three record
    types and asserts each emitted record conforms.
    """
    _write_raw_transcript(
        state_dir,
        [
            _user_input("conv-A", 0, "create test.txt"),
            _planner_response(
                "conv-A",
                2,
                text="hi there",
                tool_calls=[{"name": "write_to_file", "args": {"path": "test.txt"}}],
            ),
            _code_action("conv-A", 3, content="Created test.txt"),
        ],
    )

    _run_converter(state_dir)

    records = _read_common_events(state_dir)
    assert {r["type"] for r in records} == {"user_message", "assistant_message", "tool_result"}
    for record in records:
        assert validate_common_transcript_record(record) is None, record
