"""Tests for the gemini common_transcript.sh converter.

Exercises the script's core behaviors by running it with --single-pass in a
controlled filesystem layout. The converter reads its input from
``$MNGR_AGENT_STATE_DIR/logs/gemini_transcript/events.jsonl`` (the raw
transcript produced by ``stream_transcript.sh``), so tests seed that file
directly rather than mocking gemini's tmp-dir layout. The streamer's
project_root filtering and offset reconciliation are covered separately in
``stream_transcript_test.py``.

Each test sets up:
  - A fake agent state dir
  - A seeded raw transcript input file at logs/gemini_transcript/events.jsonl
  - A stub mngr_log.sh (no-op logging)

The --single-pass flag makes the script run one conversion pass then exit,
so tests are fast and deterministic.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

# -- Helpers --


def _make_user_event(uuid: str, timestamp: str, text: str) -> str:
    """Build a real (human-typed) gemini user event."""
    return json.dumps(
        {
            "id": uuid,
            "timestamp": timestamp,
            "type": "user",
            "content": [{"text": text}],
        }
    )


def _make_gemini_event(
    uuid: str,
    timestamp: str,
    text: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    model: str = "gemini-3-flash-preview",
    tokens: dict[str, int] | None = None,
) -> str:
    """Build a gemini assistant event, optionally with tool calls."""
    body: dict[str, Any] = {
        "id": uuid,
        "timestamp": timestamp,
        "type": "gemini",
        "content": text,
        "thoughts": [],
        "tokens": tokens or {"input": 100, "output": 50, "cached": 0, "thoughts": 0, "tool": 0, "total": 150},
        "model": model,
    }
    if tool_calls:
        body["toolCalls"] = tool_calls
    return json.dumps(body)


def _make_session_header(session_id: str, start_time: str) -> str:
    return json.dumps(
        {
            "sessionId": session_id,
            "projectHash": "deadbeef",
            "startTime": start_time,
            "lastUpdated": start_time,
            "kind": "main",
        }
    )


def _make_set_event(timestamp: str) -> str:
    return json.dumps({"$set": {"lastUpdated": timestamp}})


class ScriptRunner:
    """Helper to run gemini common_transcript.sh in a test environment."""

    def __init__(self, tmp_path: Path, stub_mngr_log_sh: str) -> None:
        self.tmp_path = tmp_path
        self.agent_state_dir = tmp_path / "agent_state"

        # Create directory structure
        self.agent_state_dir.mkdir(parents=True)
        (self.agent_state_dir / "commands").mkdir(parents=True)

        # Write stub mngr_log.sh
        log_path = self.agent_state_dir / "commands" / "mngr_log.sh"
        log_path.write_text(stub_mngr_log_sh)
        log_path.chmod(0o755)

        # Standard paths
        self.script_path = Path(__file__).parent / "common_transcript.sh"
        self.input_file = self.agent_state_dir / "logs" / "gemini_transcript" / "events.jsonl"
        self.output_file = self.agent_state_dir / "events" / "gemini" / "common_transcript" / "events.jsonl"

    def add_session(self, lines: list[str]) -> Path:
        """Seed the raw transcript input file with the given JSONL lines.

        The converter reads a flat raw stream produced by ``stream_transcript.sh``;
        project-root filtering happens upstream in the streamer, so this helper
        just appends the given lines to the input file. Returns the input file
        path so tests can append to it.
        """
        self.input_file.parent.mkdir(parents=True, exist_ok=True)
        with self.input_file.open("a") as f:
            for line in lines:
                f.write(line + "\n")
        return self.input_file

    def append_to_session(self, session_file: Path, lines: list[str]) -> None:
        with session_file.open("a") as f:
            for line in lines:
                f.write(line + "\n")

    def get_output_events(self) -> list[dict[str, Any]]:
        """Read and parse all output events."""
        if not self.output_file.exists():
            return []
        events = []
        for line in self.output_file.read_text().splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
        return events

    def run_single_pass(self, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
        """Run the script with --single-pass."""
        env = {
            **os.environ,
            "MNGR_AGENT_STATE_DIR": str(self.agent_state_dir),
        }
        return subprocess.run(
            ["bash", str(self.script_path), "--single-pass"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )


# -- Tests --


def test_no_raw_input_produces_no_output(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_events() == []


def test_converts_user_text_message(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.add_session(
        [
            _make_session_header("sid-1", "2026-01-01T00:00:00Z"),
            _make_user_event("uuid-1", "2026-01-01T00:00:01Z", "Hello"),
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "Hello"
    assert events[0]["event_id"] == "uuid-1-user"
    assert events[0]["source"] == "gemini/common_transcript"
    assert events[0]["role"] == "user"


def test_converts_assistant_message(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.add_session(
        [
            _make_session_header("sid-1", "2026-01-01T00:00:00Z"),
            _make_gemini_event("uuid-2", "2026-01-01T00:00:02Z", text="Hi there!"),
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["type"] == "assistant_message"
    assert events[0]["text"] == "Hi there!"
    assert events[0]["model"] == "gemini-3-flash-preview"
    assert events[0]["event_id"] == "uuid-2-assistant"
    assert events[0]["usage"]["input_tokens"] == 100
    assert events[0]["usage"]["output_tokens"] == 50


def test_converts_tool_calls_and_results(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """A gemini event with tool calls should emit one assistant_message plus one tool_result per call."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    tool_call = {
        "id": "tc-1",
        "name": "write_file",
        "args": {"file_path": "test.md", "content": "q"},
        "result": [
            {"functionResponse": {"id": "tc-1", "name": "write_file", "response": {"output": "wrote test.md"}}}
        ],
        "status": "success",
        "timestamp": "2026-01-01T00:00:04Z",
    }
    runner.add_session(
        [
            _make_session_header("sid-1", "2026-01-01T00:00:00Z"),
            _make_gemini_event("uuid-3", "2026-01-01T00:00:03Z", tool_calls=[tool_call]),
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 2
    assistant = next(e for e in events if e["type"] == "assistant_message")
    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert len(assistant["tool_calls"]) == 1
    assert assistant["tool_calls"][0]["tool_name"] == "write_file"
    assert assistant["tool_calls"][0]["tool_call_id"] == "tc-1"
    assert "file_path" in assistant["tool_calls"][0]["input_preview"]
    assert tool_result["tool_call_id"] == "tc-1"
    assert tool_result["tool_name"] == "write_file"
    assert tool_result["output"] == "wrote test.md"
    assert tool_result["is_error"] is False


def test_failed_tool_call_marks_is_error(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    tool_call = {
        "id": "tc-fail",
        "name": "run_shell_command",
        "args": {"command": "false"},
        "result": [
            {"functionResponse": {"id": "tc-fail", "name": "run_shell_command", "response": {"output": "boom"}}}
        ],
        "status": "error",
        "timestamp": "2026-01-01T00:00:05Z",
    }
    runner.add_session(
        [
            _make_session_header("sid-1", "2026-01-01T00:00:00Z"),
            _make_gemini_event("uuid-err", "2026-01-01T00:00:04Z", tool_calls=[tool_call]),
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert tool_result["is_error"] is True


def test_deduplicates_by_event_id(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.add_session(
        [
            _make_session_header("sid-1", "2026-01-01T00:00:00Z"),
            _make_user_event("uuid-1", "2026-01-01T00:00:01Z", "Hello"),
        ]
    )

    # Pre-populate output with the same event_id
    runner.output_file.parent.mkdir(parents=True, exist_ok=True)
    runner.output_file.write_text(
        json.dumps({"event_id": "uuid-1-user", "type": "user_message", "content": "Hello"}) + "\n"
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1


def test_skips_session_header_and_set_events(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.add_session(
        [
            _make_session_header("sid-1", "2026-01-01T00:00:00Z"),
            _make_set_event("2026-01-01T00:00:01Z"),
            _make_user_event("uuid-1", "2026-01-01T00:00:02Z", "real message"),
            _make_set_event("2026-01-01T00:00:03Z"),
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["content"] == "real message"


def test_handles_malformed_json(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.add_session(
        [
            _make_session_header("sid-1", "2026-01-01T00:00:00Z"),
            "not json",
            _make_user_event("uuid-1", "2026-01-01T00:00:01Z", "valid"),
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["content"] == "valid"


def test_skips_events_without_id(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    no_id = json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z", "content": [{"text": "hi"}]})
    runner.add_session([_make_session_header("sid-1", "2026-01-01T00:00:00Z"), no_id])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_events() == []


def test_truncates_long_tool_input_preview(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    long_args = {"content": "x" * 500}
    tool_call = {
        "id": "tc-long",
        "name": "write_file",
        "args": long_args,
        "result": [],
        "status": "success",
        "timestamp": "2026-01-01T00:00:01Z",
    }
    runner.add_session(
        [
            _make_session_header("sid-1", "2026-01-01T00:00:00Z"),
            _make_gemini_event("uuid-long", "2026-01-01T00:00:00Z", tool_calls=[tool_call]),
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assistant = next(e for e in events if e["type"] == "assistant_message")
    assert len(assistant["tool_calls"][0]["input_preview"]) <= 203


def test_truncates_long_tool_output(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    long_output = "x" * 5000
    tool_call = {
        "id": "tc-out",
        "name": "read_file",
        "args": {"file": "big.txt"},
        "result": [{"functionResponse": {"id": "tc-out", "name": "read_file", "response": {"output": long_output}}}],
        "status": "success",
        "timestamp": "2026-01-01T00:00:01Z",
    }
    runner.add_session(
        [
            _make_session_header("sid-1", "2026-01-01T00:00:00Z"),
            _make_gemini_event("uuid-bo", "2026-01-01T00:00:00Z", tool_calls=[tool_call]),
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert len(tool_result["output"]) <= 2003


def test_sorts_by_timestamp(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Events should be output sorted by timestamp."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.add_session(
        [
            _make_session_header("sid-1", "2026-01-01T00:00:00Z"),
            _make_user_event("uuid-later", "2026-01-01T00:00:02Z", "Later"),
            _make_user_event("uuid-earlier", "2026-01-01T00:00:01Z", "Earlier"),
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 2
    assert events[0]["content"] == "Earlier"
    assert events[1]["content"] == "Later"


def test_cache_read_tokens_captured(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Gemini's `cached` token field is mapped to cache_read_tokens in the common schema."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.add_session(
        [
            _make_session_header("sid-1", "2026-01-01T00:00:00Z"),
            _make_gemini_event(
                "uuid-cache",
                "2026-01-01T00:00:00Z",
                text="hi",
                tokens={"input": 100, "output": 50, "cached": 80, "thoughts": 0, "tool": 0, "total": 150},
            ),
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert events[0]["usage"]["cache_read_tokens"] == 80


def test_output_writes_to_correct_path(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Output should go to events/gemini/common_transcript/events.jsonl."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.add_session(
        [
            _make_session_header("sid-1", "2026-01-01T00:00:00Z"),
            _make_user_event("uuid-1", "2026-01-01T00:00:01Z", "Hello"),
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    expected_path = runner.agent_state_dir / "events" / "gemini" / "common_transcript" / "events.jsonl"
    assert expected_path.exists()
    assert len(expected_path.read_text().strip().splitlines()) == 1


def test_incremental_conversion(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Running twice with new input should append without duplicates."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    session_file = runner.add_session(
        [
            _make_session_header("sid-1", "2026-01-01T00:00:00Z"),
            _make_user_event("uuid-1", "2026-01-01T00:00:01Z", "First"),
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert len(runner.get_output_events()) == 1

    # Append a new event to the session file
    runner.append_to_session(session_file, [_make_user_event("uuid-2", "2026-01-01T00:00:02Z", "Second")])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 2
    assert events[0]["content"] == "First"
    assert events[1]["content"] == "Second"


def test_events_from_multiple_sessions_in_one_raw_stream(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """The converter reads a flat raw stream, so events from multiple sessions concatenate naturally."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.add_session(
        [
            _make_session_header("sid-1", "2026-01-01T00:00:00Z"),
            _make_user_event("uuid-1", "2026-01-01T00:00:01Z", "session A"),
            _make_session_header("sid-2", "2026-01-01T00:00:02Z"),
            _make_user_event("uuid-2", "2026-01-01T00:00:03Z", "session B"),
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    contents = {e["content"] for e in events}
    assert contents == {"session A", "session B"}
