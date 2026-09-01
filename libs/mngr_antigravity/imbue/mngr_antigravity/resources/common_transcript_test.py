"""Tests for the antigravity common_transcript.sh converter.

Exercises the script's core behaviors by running it with --single-pass in a
controlled filesystem layout. The converter reads its input from
``$MNGR_AGENT_STATE_DIR/logs/antigravity_transcript/events.jsonl`` (the raw
transcript produced by ``stream_transcript.sh``), so tests seed that file
directly rather than running agy. Each event in the seeded raw transcript
must carry the ``_mngr_conv_id`` field that the streamer adds; this is the
key the converter uses to scope tool-call/result pairing within a single
conversation. The output is the ATIF-shaped record stream (``header`` /
``step`` / ``observation``).

These are the shell-level tests: they cover what only running the script can
show -- that the pass reaches the converter, stays silent on the watcher's
stdout/stderr, and serializes on the convert lock. The event-by-event
conversion rules are asserted directly against ``convert`` in
common_transcript_convert_test.py.

Each test sets up:
  - A fake agent state dir at tmp_path/agent
  - A stub mngr_log.sh in commands/
  - A seeded raw transcript at logs/antigravity_transcript/events.jsonl
  - Runs the converter once via --single-pass and inspects the output
"""

from __future__ import annotations

import importlib.resources
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr import resources as mngr_resources
from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record
from imbue.mngr_antigravity.resources.testing import code_action_event as _code_action
from imbue.mngr_antigravity.resources.testing import conversation_history_event as _conversation_history
from imbue.mngr_antigravity.resources.testing import planner_response_event as _planner_response
from imbue.mngr_antigravity.resources.testing import transcript_event as _make_event
from imbue.mngr_antigravity.resources.testing import user_input_event as _user_input

_SCRIPT_PATH = Path(__file__).parent / "common_transcript.sh"


@pytest.fixture
def state_dir(tmp_path: Path, stub_mngr_log_sh: str) -> Path:
    """Per-test fake $MNGR_AGENT_STATE_DIR with stub mngr_log.sh + the real
    shared common-transcript lib installed (the converter sources it for the
    convert lock), mirroring Host._ensure_shared_shell_libs."""
    state = tmp_path / "agent"
    (state / "commands").mkdir(parents=True)
    (state / "logs" / "antigravity_transcript").mkdir(parents=True)
    (state / "commands" / "mngr_log.sh").write_text(stub_mngr_log_sh)
    (state / "commands" / "mngr_common_transcript_lib.sh").write_text(
        importlib.resources.files(mngr_resources).joinpath("mngr_common_transcript_lib.sh").read_text()
    )
    return state


def _write_raw_transcript(state_dir: Path, lines: list[Any]) -> None:
    """Seed the raw transcript; a line is either a built event or raw text."""
    raw_path = state_dir / "logs" / "antigravity_transcript" / "events.jsonl"
    raw_path.write_text("\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n")


def _run_converter(state_dir: Path) -> str:
    """Run common_transcript.sh in single-pass mode against the seeded raw transcript.

    A clean run stays silent: the converter's count is captured by the shell and
    any genuine error is logged to events/logs/common_transcript, never echoed to
    stdout/stderr. Returns stderr so the guard below can flag anything the script
    unexpectedly writes there (e.g. a shell-level failure that drops events and
    would otherwise fail a downstream assertion mysteriously).
    """
    env = {**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)}
    result = subprocess.run(
        ["bash", str(_SCRIPT_PATH), "--single-pass"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Traceback" not in result.stderr, result.stderr
    return result.stderr


def _run_single_pass(state_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run one pass and return the full process so callers can inspect stdout/stderr."""
    return subprocess.run(
        ["bash", str(_SCRIPT_PATH), "--single-pass"],
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)},
        capture_output=True,
        text=True,
        check=True,
    )


def _read_common_records(state_dir: Path) -> list[dict[str, Any]]:
    output_path = state_dir / "events" / "antigravity" / "common_transcript" / "events.jsonl"
    if not output_path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in output_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


def _steps(state_dir: Path) -> list[dict[str, Any]]:
    return [r for r in _read_common_records(state_dir) if r["type"] == "step"]


def _observations(state_dir: Path) -> list[dict[str, Any]]:
    return [r for r in _read_common_records(state_dir) if r["type"] == "observation"]


# -- Tests --


def test_user_input_is_converted_to_a_user_step(state_dir: Path) -> None:
    """USER_EXPLICIT/USER_INPUT -> user step carrying agy's clean typed text.

    agy's SQLite store (via decode_agy_transcript.py) records the bare typed text in
    ``CortexStepUserInput.query``; the converter passes it through, stripped of surrounding
    whitespace, with no envelope handling.
    """
    _write_raw_transcript(state_dir, [_user_input("conv-A", 0, "What is 2+2?")])

    _run_converter(state_dir)

    steps = _steps(state_dir)
    assert len(steps) == 1
    step = steps[0]
    assert step["source"] == "user"
    assert step["message"] == "What is 2+2?"
    assert step["extra"] == {"conversation_id": "conv-A", "step_index": 0}
    assert step["emitter"] == "antigravity/common_transcript"


def test_planner_response_without_tool_calls_is_an_agent_step(state_dir: Path) -> None:
    _write_raw_transcript(
        state_dir,
        [
            _user_input("conv-A", 0, "hi"),
            _planner_response("conv-A", 2, text="Hello back."),
        ],
    )

    _run_converter(state_dir)

    steps = _steps(state_dir)
    assert [s["source"] for s in steps] == ["user", "agent"]
    assert steps[1]["message"] == "Hello back."
    assert "tool_calls" not in steps[1]
    assert steps[1]["extra"]["conversation_id"] == "conv-A"


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
                    {"name": "write_to_file", "args": json.dumps({"path": "/tmp/x", "content": "hi"})},
                ],
            ),
        ],
    )

    _run_converter(state_dir)

    assert _steps(state_dir)[1]["tool_calls"] == [
        {
            "tool_call_id": "conv-A-2-tc0",
            "function_name": "write_to_file",
            "arguments": {"path": "/tmp/x", "content": "hi"},
        }
    ]


def test_code_action_pairs_with_preceding_planner_response_tool_call(state_dir: Path) -> None:
    """MODEL/CODE_ACTION -> observation whose source_call_id matches the last tool call in the same conversation."""
    _write_raw_transcript(
        state_dir,
        [
            _user_input("conv-A", 0, "create test.txt"),
            _planner_response(
                "conv-A",
                2,
                tool_calls=[{"name": "write_to_file", "args": json.dumps({"path": "test.txt"})}],
            ),
            _code_action("conv-A", 3, "Created test.txt"),
        ],
    )

    _run_converter(state_dir)

    assert [s["source"] for s in _steps(state_dir)] == ["user", "agent"]
    assert _observations(state_dir)[0]["results"] == [
        {
            "source_call_id": "conv-A-2-tc0",
            "content": "Created test.txt",
            "extra": {
                "is_error": False,
                "tool_name": "write_to_file",
                "conversation_id": "conv-A",
                "step_index": 3,
            },
        }
    ]


def test_planner_response_with_multiple_tool_calls_pairs_last_with_code_action(state_dir: Path) -> None:
    """A multi-tool-call PLANNER_RESPONSE lists every tool_call but only the last one
    gets paired with the subsequent CODE_ACTION.

    This documents the converter's current behavior: agy emits one CODE_ACTION
    per planner response (per the converter module's docstring) regardless of
    how many tool_calls the response contained, so only the last tool_call has
    a matching observation. The earlier tool_calls still appear in the step's
    tool_calls array. If agy's emit pattern ever changes to one CODE_ACTION per
    tool_call, this test (and the pairing logic) will need to be revisited.
    """
    _write_raw_transcript(
        state_dir,
        [
            _user_input("conv-A", 0, "do two things"),
            _planner_response(
                "conv-A",
                2,
                tool_calls=[
                    {"name": "first_tool", "args": json.dumps({"a": 1})},
                    {"name": "second_tool", "args": json.dumps({"b": 2})},
                ],
            ),
            _code_action("conv-A", 3, "paired output"),
        ],
    )

    _run_converter(state_dir)

    agent_step = next(s for s in _steps(state_dir) if s["source"] == "agent")
    assert [tc["function_name"] for tc in agent_step["tool_calls"]] == ["first_tool", "second_tool"]
    assert [tc["tool_call_id"] for tc in agent_step["tool_calls"]] == ["conv-A-2-tc0", "conv-A-2-tc1"]

    observations = _observations(state_dir)
    assert len(observations) == 1
    # The CODE_ACTION pairs with the LAST tool_call (tc1), not the first.
    assert observations[0]["results"][0]["source_call_id"] == "conv-A-2-tc1"
    assert observations[0]["results"][0]["extra"]["tool_name"] == "second_tool"


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

    assert [s["source"] for s in _steps(state_dir)] == ["user", "agent"]


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

    assert [s["source"] for s in _steps(state_dir)] == ["user", "agent"]


def test_tool_call_pairing_is_scoped_per_conversation(state_dir: Path) -> None:
    """Conv A's CODE_ACTION must not pair with Conv B's tool call (or vice versa)."""
    _write_raw_transcript(
        state_dir,
        [
            _planner_response("conv-A", 2, tool_calls=[{"name": "tool_a", "args": "{}"}]),
            _planner_response("conv-B", 2, tool_calls=[{"name": "tool_b", "args": "{}"}]),
            # Pair conv-A's CODE_ACTION; if conv-A's pending call leaked into conv-B
            # bucket the pairing would be wrong.
            _code_action("conv-A", 3, "result_a"),
            _code_action("conv-B", 3, "result_b"),
        ],
    )

    _run_converter(state_dir)

    results = [result for o in _observations(state_dir) for result in o["results"]]
    assert {result["extra"]["tool_name"]: result["content"] for result in results} == {
        "tool_a": "result_a",
        "tool_b": "result_b",
    }


def test_converter_appends_only_new_records_on_incremental_runs(state_dir: Path) -> None:
    """A second pass with extra raw events appends only the new ones."""
    _write_raw_transcript(state_dir, [_user_input("conv-A", 0, "first")])
    _run_converter(state_dir)
    assert len(_steps(state_dir)) == 1

    _write_raw_transcript(
        state_dir,
        [
            _user_input("conv-A", 0, "first"),
            _planner_response("conv-A", 2, text="second"),
        ],
    )
    _run_converter(state_dir)
    assert [s["source"] for s in _steps(state_dir)] == ["user", "agent"]


def test_missing_output_file_emits_nothing_to_pane(state_dir: Path) -> None:
    """On the first pass the output file does not exist yet; the watcher must
    stay completely silent on stdout/stderr while still converting the event.
    The converter's count is captured by the shell, never echoed to the pane.
    """
    _write_raw_transcript(state_dir, [_user_input("conv-A", 0, "Hello")])
    output_path = state_dir / "events" / "antigravity" / "common_transcript" / "events.jsonl"
    assert not output_path.exists()

    result = _run_single_pass(state_dir)
    assert result.stdout == "", f"unexpected stdout: {result.stdout!r}"
    assert result.stderr == "", f"unexpected stderr: {result.stderr!r}"
    assert len(_steps(state_dir)) == 1


def test_dropped_lines_emit_nothing_to_pane(state_dir: Path) -> None:
    """Malformed lines are dropped silently and must produce no output on the
    watcher's stdout/stderr; the valid line still converts.
    """
    _write_raw_transcript(state_dir, ["{ not valid json", _user_input("conv-A", 0, "kept")])

    result = _run_single_pass(state_dir)
    assert result.stdout == "", f"unexpected stdout: {result.stdout!r}"
    assert result.stderr == "", f"unexpected stderr: {result.stderr!r}"
    assert [s["message"] for s in _steps(state_dir)] == ["kept"]


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

    ids = sorted(s["event_id"] for s in _steps(state_dir))
    assert ids == ["conv-A-0-user", "conv-B-0-user"]


def test_emitted_common_records_conform_to_canonical_schema(state_dir: Path) -> None:
    """Every record antigravity's converter emits must validate against the canonical schema.

    Guards against the antigravity emitter (common_transcript.sh) and the canonical schema
    (imbue.mngr.agents.common_transcript_records) drifting apart. Drives every record type
    the emitter can produce and asserts each emitted record conforms.
    """
    _write_raw_transcript(
        state_dir,
        [
            _user_input("conv-A", 0, "create test.txt"),
            _planner_response(
                "conv-A",
                2,
                text="hi there",
                thinking="a file needs writing",
                tool_calls=[{"name": "write_to_file", "args": json.dumps({"path": "test.txt"})}],
            ),
            _code_action("conv-A", 3, "Created test.txt"),
        ],
    )

    _run_converter(state_dir)

    records = _read_common_records(state_dir)
    assert {r["type"] for r in records} <= {"header", "step", "observation"}
    assert {"header", "step"} <= {r["type"] for r in records}
    for record in records:
        assert validate_common_transcript_record(record) is None, record


# -- Convert-lock serialization (shared by the 5s daemon and on-demand flush) --


def _lock_dir(state_dir: Path) -> Path:
    return state_dir / ".common_transcript_convert.lock"


def test_held_convert_lock_skips_pass(state_dir: Path) -> None:
    """A pass that cannot take the convert lock (held by a concurrent pass)
    skips conversion rather than racing into duplicate output. Simulated by
    pre-creating the (fresh) lock dir and giving the pass a short timeout."""
    _write_raw_transcript(state_dir, [_user_input("conv-A", 0, "hello")])
    _lock_dir(state_dir).mkdir(parents=True)

    env = {**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir), "MNGR_CONVERT_LOCK_TIMEOUT": "1"}
    result = subprocess.run(
        ["bash", str(_SCRIPT_PATH), "--single-pass"], env=env, capture_output=True, text=True, check=True
    )
    assert "Traceback" not in result.stderr, result.stderr
    assert _read_common_records(state_dir) == []

    _lock_dir(state_dir).rmdir()
    _run_converter(state_dir)
    assert len(_steps(state_dir)) == 1


def test_stale_convert_lock_is_broken(state_dir: Path) -> None:
    """A convert lock older than a minute is treated as stale and broken, so the
    converter never wedges permanently."""
    _write_raw_transcript(state_dir, [_user_input("conv-A", 0, "hello")])
    lock = _lock_dir(state_dir)
    lock.mkdir(parents=True)
    stale = time.time() - 120
    os.utime(lock, (stale, stale))

    env = {**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir), "MNGR_CONVERT_LOCK_TIMEOUT": "1"}
    result = subprocess.run(
        ["bash", str(_SCRIPT_PATH), "--single-pass"], env=env, capture_output=True, text=True, check=True
    )
    assert "Traceback" not in result.stderr, result.stderr
    assert len(_steps(state_dir)) == 1


def test_concurrent_passes_do_not_duplicate(state_dir: Path) -> None:
    """Two passes racing over the same input must not both append the same
    records: the lock serializes them so the second sees the first's output in
    its dedup set. Without the lock this produces duplicate event_ids."""
    _write_raw_transcript(state_dir, [_user_input(f"conv-{i}", i, f"msg {i}") for i in range(20)])

    env = {**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)}
    procs = [
        subprocess.Popen(
            ["bash", str(_SCRIPT_PATH), "--single-pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        for _ in range(2)
    ]
    for proc in procs:
        assert proc.wait(timeout=30) == 0

    records = _read_common_records(state_dir)
    event_ids = [r["event_id"] for r in records]
    assert len(event_ids) == len(set(event_ids)), "convert lock failed to prevent duplicate events"
    assert len(_steps(state_dir)) == 20
