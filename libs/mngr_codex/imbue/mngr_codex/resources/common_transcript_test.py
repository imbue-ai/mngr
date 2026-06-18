"""Tests for the codex common_transcript.sh converter.

Exercises the converter with --single-pass in a controlled filesystem layout.
The converter reads its input from
``$MNGR_AGENT_STATE_DIR/logs/codex_transcript/events.jsonl`` (the verbatim
rollout stream produced by stream_transcript.sh), so tests seed that file
directly rather than running codex. Each line is a codex rollout record of the
form ``{"timestamp":..,"type":<t>,"payload":<p>}``.

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

_SCRIPT_PATH = Path(__file__).parent / "common_transcript.sh"


def _line(type_: str, payload: dict[str, Any], timestamp: str = "2026-06-09T07:00:00.000Z") -> str:
    return json.dumps({"timestamp": timestamp, "type": type_, "payload": payload})


def _user(text: str) -> str:
    return _line(
        "response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
    )


def _assistant(text: str) -> str:
    return _line(
        "response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    )


def _function_call(name: str, arguments: str, call_id: str) -> str:
    return _line("response_item", {"type": "function_call", "name": name, "arguments": arguments, "call_id": call_id})


def _function_call_output(call_id: str, output: Any) -> str:
    return _line("response_item", {"type": "function_call_output", "call_id": call_id, "output": output})


def _event_msg_user(text: str) -> str:
    """The display-duplicate event_msg codex also writes for each user message."""
    return _line("event_msg", {"type": "user_message", "message": text, "images": []})


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


def _write_raw_stream(state_dir: Path, lines: list[str]) -> None:
    raw_path = state_dir / "logs" / "codex_transcript" / "events.jsonl"
    raw_path.write_text("\n".join(lines) + "\n")


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


def _read_common_events(state_dir: Path) -> list[dict[str, Any]]:
    output_path = state_dir / "events" / "codex" / "common_transcript" / "events.jsonl"
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


def test_user_message_is_converted(state_dir: Path) -> None:
    """response_item/message/user -> user_message with joined input_text."""
    _write_raw_stream(state_dir, [_user("What is 2+2?")])

    _run_converter(state_dir)

    events = _read_common_events(state_dir)
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "user_message"
    assert event["role"] == "user"
    assert event["content"] == "What is 2+2?"
    assert event["source"] == "codex/common_transcript"


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

    events = _read_common_events(state_dir)
    assert events[0]["content"] == "part one part two"


def test_assistant_message_is_converted(state_dir: Path) -> None:
    """response_item/message/assistant -> assistant_message with joined output_text."""
    _write_raw_stream(state_dir, [_user("hi"), _assistant("Hello back.")])

    _run_converter(state_dir)

    events = _read_common_events(state_dir)
    assert [e["type"] for e in events] == ["user_message", "assistant_message"]
    assistant = events[1]
    assert assistant["role"] == "assistant"
    assert assistant["text"] == "Hello back."
    assert assistant["tool_calls"] == []


def test_function_call_emits_assistant_message_with_tool_call(state_dir: Path) -> None:
    """function_call -> assistant_message whose tool_calls carry the invocation.

    codex models the call as a standalone rollout item (no assistant `message`),
    so the converter surfaces it on an assistant turn -- matching the other ports.
    The assistant tool_call_id must match the paired tool_result's.
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

    events = _read_common_events(state_dir)
    assert [e["type"] for e in events] == ["user_message", "assistant_message", "tool_result"]
    assistant, tool_result = events[1], events[2]
    assert assistant["text"] == ""
    assert len(assistant["tool_calls"]) == 1
    call = assistant["tool_calls"][0]
    assert call["tool_name"] == "shell_command"
    assert call["input_preview"] == '{"command":"ls"}'
    assert tool_result["tool_name"] == "shell_command"
    assert tool_result["output"] == "file_a\nfile_b\n"
    assert tool_result["is_error"] is False
    # The assistant tool_call and its tool_result share the synthetic id minted on
    # the function_call line, so a reader can pair them.
    assert call["tool_call_id"] == "line-2-tc"
    assert tool_result["tool_call_id"] == "line-2-tc"


def test_function_call_output_as_content_array_is_stringified(state_dir: Path) -> None:
    """function_call_output.output can be an array of content items, not just a string."""
    _write_raw_stream(
        state_dir,
        [
            _function_call("read_file", "{}", "call_arr"),
            _function_call_output(
                "call_arr",
                [{"type": "output_text", "text": "line1\n"}, {"type": "output_text", "text": "line2"}],
            ),
        ],
    )

    _run_converter(state_dir)

    events = _read_common_events(state_dir)
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["output"] == "line1\nline2"


def test_function_call_output_without_matching_call_is_dropped(state_dir: Path) -> None:
    """A bare function_call_output (no preceding function_call) has nothing to pair with."""
    _write_raw_stream(state_dir, [_function_call_output("call_orphan", "orphan output")])

    _run_converter(state_dir)

    assert _read_common_events(state_dir) == []


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

    events = _read_common_events(state_dir)
    types = [e["type"] for e in events]
    # Exactly one user_message and one assistant_message -- the event_msg is dropped.
    assert types == ["user_message", "assistant_message"]


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

    events = _read_common_events(state_dir)
    assert [e["type"] for e in events] == ["user_message", "assistant_message"]


def test_empty_user_message_is_dropped(state_dir: Path) -> None:
    """A message with no input_text carries no signal."""
    raw = _line(
        "response_item", {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "x"}]}
    )
    _write_raw_stream(state_dir, [raw])

    _run_converter(state_dir)

    assert _read_common_events(state_dir) == []


def test_emitted_common_records_conform_to_canonical_schema(state_dir: Path) -> None:
    """Every record codex's converter emits must validate against the shared envelope schema.

    Guards against the codex emitter (common_transcript.sh) and the canonical schema
    (imbue.mngr.agents.common_transcript_records) drifting apart. Drives all three record
    types and asserts each emitted record conforms.
    """
    _write_raw_stream(
        state_dir,
        [
            _user("hello"),
            _assistant("hi there"),
            _function_call("shell", '{"command": "ls"}', "call_1"),
            _function_call_output("call_1", "file.txt"),
        ],
    )
    _run_converter(state_dir)
    records = _read_common_events(state_dir)
    assert {r["type"] for r in records} == {"user_message", "assistant_message", "tool_result"}
    for record in records:
        assert validate_common_transcript_record(record) is None, record


def test_converter_is_idempotent_across_runs(state_dir: Path) -> None:
    """Re-running on the same input must not duplicate events (dedupe by event_id)."""
    _write_raw_stream(state_dir, [_user("hi"), _assistant("hello")])

    _run_converter(state_dir)
    first = _read_common_events(state_dir)
    _run_converter(state_dir)
    second = _read_common_events(state_dir)

    assert first == second
    assert len(second) == 2


def test_converter_appends_only_new_events_on_incremental_runs(state_dir: Path) -> None:
    """A second pass with extra raw lines appends only the new ones."""
    _write_raw_stream(state_dir, [_user("first")])
    _run_converter(state_dir)
    assert len(_read_common_events(state_dir)) == 1

    _write_raw_stream(state_dir, [_user("first"), _assistant("second")])
    _run_converter(state_dir)
    second_pass = _read_common_events(state_dir)
    assert [e["type"] for e in second_pass] == ["user_message", "assistant_message"]


def test_malformed_lines_are_skipped_not_fatal(state_dir: Path) -> None:
    """A truncated / partial JSON line shouldn't abort the rest of the conversion."""
    raw_path = state_dir / "logs" / "codex_transcript" / "events.jsonl"
    raw_path.write_text("{ not valid json\n" + _user("after the broken line") + "\n")

    _run_converter(state_dir)

    events = _read_common_events(state_dir)
    assert len(events) == 1
    assert events[0]["content"] == "after the broken line"


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
    assert len(_read_common_events(state_dir)) == 1


def test_dropped_lines_emit_nothing_to_pane(state_dir: Path) -> None:
    """Malformed lines are dropped silently and must produce no output on the
    watcher's stdout/stderr; the valid line still converts.
    """
    _write_raw_stream(state_dir, ["not json", _user("kept")])

    result = _run_single_pass(state_dir)
    assert result.stdout == "", f"unexpected stdout: {result.stdout!r}"
    assert result.stderr == "", f"unexpected stderr: {result.stderr!r}"
    events = _read_common_events(state_dir)
    assert [e["content"] for e in events] == ["kept"]


def test_event_ids_are_stable_per_line(state_dir: Path) -> None:
    """Event ids derive from the line index, so they're stable across restarts."""
    _write_raw_stream(state_dir, [_user("a-message"), _assistant("b-message")])

    _run_converter(state_dir)

    events = _read_common_events(state_dir)
    ids = [e["event_id"] for e in events]
    assert ids == ["line-1-user", "line-2-assistant"]


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
    assert _read_common_events(state_dir) == []

    _lock_dir(state_dir).rmdir()
    _run_converter(state_dir)
    assert len(_read_common_events(state_dir)) == 1


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
    assert len(_read_common_events(state_dir)) == 1


def test_concurrent_passes_do_not_duplicate(state_dir: Path) -> None:
    """Two passes racing over the same input must not both append the same
    events: the lock serializes them so the second sees the first's output in
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

    events = _read_common_events(state_dir)
    event_ids = [e["event_id"] for e in events]
    assert len(event_ids) == len(set(event_ids)), "convert lock failed to prevent duplicate events"
    assert len(events) == 20
