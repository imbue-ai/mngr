"""Tests for the codex common_transcript.sh converter.

Exercises the converter with --single-pass in a controlled filesystem layout.
The converter reads its input from
``$MNGR_AGENT_STATE_DIR/logs/codex_transcript/events.jsonl`` (the verbatim
rollout stream produced by stream_transcript.sh), so tests seed that file
directly rather than running codex. Each line is a codex rollout record of the
form ``{"timestamp":..,"type":<t>,"payload":<p>}``; the output is the
ATIF-shaped record stream (``header`` / ``step`` / ``observation``).

These are the shell-level tests: they cover what only running the script can
show -- that the pass reaches the converter, stays silent on the watcher's
stdout/stderr, and serializes on the convert lock. The record-by-record
conversion rules are asserted directly against ``convert`` in
common_transcript_convert_test.py.

Each test sets up:
  - A fake agent state dir at tmp_path/agent
  - A stub mngr_log.sh in commands/
  - A seeded raw rollout stream at logs/codex_transcript/events.jsonl
  - Runs the converter once via --single-pass and inspects the common output
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
from imbue.mngr_codex.resources.testing import rollout_assistant_message as _assistant
from imbue.mngr_codex.resources.testing import rollout_event_msg_user as _event_msg_user
from imbue.mngr_codex.resources.testing import rollout_function_call as _function_call
from imbue.mngr_codex.resources.testing import rollout_function_call_output as _function_call_output
from imbue.mngr_codex.resources.testing import rollout_line as _line
from imbue.mngr_codex.resources.testing import rollout_reasoning as _reasoning
from imbue.mngr_codex.resources.testing import rollout_user_message as _user

_SCRIPT_PATH = Path(__file__).parent / "common_transcript.sh"


@pytest.fixture
def state_dir(tmp_path: Path, stub_mngr_log_sh: str) -> Path:
    """Per-test fake $MNGR_AGENT_STATE_DIR with stub mngr_log.sh + the real
    shared common-transcript lib installed (the converter sources it for the
    convert lock), mirroring Host._ensure_shared_shell_libs."""
    state = tmp_path / "agent"
    (state / "commands").mkdir(parents=True)
    (state / "logs" / "codex_transcript").mkdir(parents=True)
    (state / "commands" / "mngr_log.sh").write_text(stub_mngr_log_sh)
    (state / "commands" / "mngr_common_transcript_lib.sh").write_text(
        importlib.resources.files(mngr_resources).joinpath("mngr_common_transcript_lib.sh").read_text()
    )
    return state


def _write_raw_stream(state_dir: Path, lines: list[Any]) -> None:
    """Seed the raw rollout stream; a line is either a built record or raw text."""
    raw_path = state_dir / "logs" / "codex_transcript" / "events.jsonl"
    raw_path.write_text("\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n")


def _run_converter(state_dir: Path) -> str:
    result = subprocess.run(
        ["bash", str(_SCRIPT_PATH), "--single-pass"],
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)},
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
    output_path = state_dir / "events" / "codex" / "common_transcript" / "events.jsonl"
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


def test_user_message_is_converted(state_dir: Path) -> None:
    """response_item/message/user -> user step with joined input_text."""
    _write_raw_stream(state_dir, [_user("What is 2+2?")])

    _run_converter(state_dir)

    steps = _steps(state_dir)
    assert len(steps) == 1
    step = steps[0]
    assert step["source"] == "user"
    assert step["message"] == "What is 2+2?"
    assert step["emitter"] == "codex/common_transcript"


def test_user_message_joins_multiple_input_text_items(state_dir: Path) -> None:
    raw = _line(
        "response_item",
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "part one "},
                {"type": "input_image", "image_url": "x"},
                {"type": "input_text", "text": "part two"},
            ],
        },
    )
    _write_raw_stream(state_dir, [raw])

    _run_converter(state_dir)

    assert _steps(state_dir)[0]["message"] == "part one part two"


def test_assistant_message_is_converted(state_dir: Path) -> None:
    """response_item/message/assistant -> agent step with joined output_text."""
    _write_raw_stream(state_dir, [_user("hi"), _assistant("Hello back.")])

    _run_converter(state_dir)

    steps = _steps(state_dir)
    assert [s["source"] for s in steps] == ["user", "agent"]
    assert steps[1]["message"] == "Hello back."
    assert "tool_calls" not in steps[1]


def test_function_call_emits_agent_step_with_native_tool_call_id(state_dir: Path) -> None:
    """function_call -> agent step whose tool_calls carry the invocation.

    codex models the call as a standalone rollout item (no assistant `message`),
    so the converter surfaces it as its own bare agent step. The tool_call_id is
    codex's own call_id, which the paired observation result points back to.
    """
    _write_raw_stream(
        state_dir,
        [
            _user("run ls"),
            _function_call("shell_command", '{"command":"ls"}', "call_xyz"),
            _function_call_output("call_xyz", "file_a\nfile_b\n"),
        ],
    )

    _run_converter(state_dir)

    steps = _steps(state_dir)
    assert [s["source"] for s in steps] == ["user", "agent"]
    assert steps[1]["message"] == ""
    assert steps[1]["tool_calls"] == [
        {"tool_call_id": "call_xyz", "function_name": "shell_command", "arguments": {"command": "ls"}}
    ]
    assert _observations(state_dir)[0]["results"] == [
        {
            "source_call_id": "call_xyz",
            "content": "file_a\nfile_b\n",
            "extra": {"is_error": False, "tool_name": "shell_command"},
        }
    ]


def test_event_msg_duplicates_are_ignored(state_dir: Path) -> None:
    """type=event_msg mirrors response_items and would double every message; ignore it."""
    _write_raw_stream(
        state_dir,
        [
            _user("Add a docstring"),
            _event_msg_user("Add a docstring"),
            _assistant("Done."),
        ],
    )

    _run_converter(state_dir)

    # Exactly one user step and one agent step -- the event_msg is dropped.
    assert [s["source"] for s in _steps(state_dir)] == ["user", "agent"]


def test_bookkeeping_records_are_dropped(state_dir: Path) -> None:
    """session_meta / turn_context / token_count are not conversation content."""
    _write_raw_stream(
        state_dir,
        [
            _line("session_meta", {"id": "019ae614-d626-70f1-a87d-31e6966231f5", "cwd": "/tmp/ws"}),
            _user("hi"),
            _line("turn_context", {"cwd": "/tmp/ws", "model": "gpt-5.1"}),
            _assistant("hello"),
        ],
    )

    _run_converter(state_dir)

    assert [s["source"] for s in _steps(state_dir)] == ["user", "agent"]


def test_emitted_common_records_conform_to_canonical_schema(state_dir: Path) -> None:
    """Every record codex's converter emits must validate against the canonical schema.

    Guards against the codex emitter (common_transcript.sh) and the canonical schema
    (imbue.mngr.agents.common_transcript_records) drifting apart. Drives every record
    type the emitter can produce and asserts each emitted record conforms.
    """
    _write_raw_stream(
        state_dir,
        [
            _user("<environment_context>\ncwd: /tmp\n</environment_context>"),
            _user("hello"),
            _reasoning("thinking it through"),
            _assistant("hi there"),
            _function_call("shell", '{"command": "ls"}', "call_1"),
            _function_call_output("call_1", "file.txt"),
        ],
    )
    _run_converter(state_dir)
    records = _read_common_records(state_dir)
    assert {r["type"] for r in records} <= {"header", "step", "observation"}
    assert {"header", "step"} <= {r["type"] for r in records}
    for record in records:
        assert validate_common_transcript_record(record) is None, record


def test_converter_appends_only_new_records_on_incremental_runs(state_dir: Path) -> None:
    """A second pass with extra raw lines appends only the new ones."""
    _write_raw_stream(state_dir, [_user("first")])
    _run_converter(state_dir)
    assert len(_steps(state_dir)) == 1

    _write_raw_stream(state_dir, [_user("first"), _assistant("second")])
    _run_converter(state_dir)
    assert [s["source"] for s in _steps(state_dir)] == ["user", "agent"]


def test_missing_output_file_emits_nothing_to_pane(state_dir: Path) -> None:
    """On the first pass the output file does not exist yet; the watcher must
    stay completely silent on stdout/stderr while still converting the event.
    The converter's count is captured by the shell, never echoed to the pane.
    """
    _write_raw_stream(state_dir, [_user("Hello")])
    output_path = state_dir / "events" / "codex" / "common_transcript" / "events.jsonl"
    assert not output_path.exists()

    result = _run_single_pass(state_dir)
    assert result.stdout == "", f"unexpected stdout: {result.stdout!r}"
    assert result.stderr == "", f"unexpected stderr: {result.stderr!r}"
    assert len(_steps(state_dir)) == 1


def test_dropped_lines_emit_nothing_to_pane(state_dir: Path) -> None:
    """Malformed lines are dropped silently and must produce no output on the
    watcher's stdout/stderr; the valid line still converts.
    """
    _write_raw_stream(state_dir, ["not json", _user("kept")])

    result = _run_single_pass(state_dir)
    assert result.stdout == "", f"unexpected stdout: {result.stdout!r}"
    assert result.stderr == "", f"unexpected stderr: {result.stderr!r}"
    assert [s["message"] for s in _steps(state_dir)] == ["kept"]


def test_event_ids_are_unique_and_stable_across_runs(state_dir: Path) -> None:
    """Event ids hash the line's own timestamp and content, so they are unique per
    record and a re-run yields the same ids and appends nothing."""
    _write_raw_stream(state_dir, [_user("a-message"), _assistant("b-message")])

    _run_converter(state_dir)
    first_ids = [r["event_id"] for r in _read_common_records(state_dir)]
    _run_converter(state_dir)
    second_ids = [r["event_id"] for r in _read_common_records(state_dir)]

    assert first_ids[0].startswith("header-")
    assert len(set(first_ids)) == 3
    assert all(event_id.startswith("evt-") for event_id in first_ids[1:])
    assert second_ids == first_ids


def _lock_dir(state_dir: Path) -> Path:
    return state_dir / ".common_transcript_convert.lock"


def test_held_convert_lock_skips_pass(state_dir: Path) -> None:
    """A pass that cannot take the convert lock (held by a concurrent pass)
    skips conversion rather than racing into duplicate output. Simulated by
    pre-creating the (fresh) lock dir and giving the pass a short timeout."""
    _write_raw_stream(state_dir, [_user("hello")])
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
    _write_raw_stream(state_dir, [_user("hello")])
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
    _write_raw_stream(state_dir, [_user(f"msg {i}") for i in range(20)])

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
