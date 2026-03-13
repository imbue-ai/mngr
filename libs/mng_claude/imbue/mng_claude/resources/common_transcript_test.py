"""Tests for common_transcript.sh.

Exercises the script's core behaviors by running it with --single-pass in a
controlled filesystem layout. Each test sets up:
  - A fake agent state dir with a raw claude transcript input file
  - A stub mng_log.sh (no-op logging)

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


def _make_assistant_event(
    uuid: str,
    timestamp: str,
    text: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    model: str = "claude-opus-4.6",
    stop_reason: str = "end_turn",
    usage: dict[str, int] | None = None,
) -> str:
    content_blocks: list[dict[str, object]] = []
    if text:
        content_blocks.append({"type": "text", "text": text})
    if tool_calls:
        for tc in tool_calls:
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": tc.get("input", {}),
                }
            )
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": timestamp,
            "message": {
                "role": "assistant",
                "model": model,
                "content": content_blocks,
                "stop_reason": stop_reason,
                "usage": usage or {"input_tokens": 100, "output_tokens": 50},
            },
        }
    )


def _make_user_event(
    uuid: str,
    timestamp: str,
    text: str = "",
    tool_results: list[dict[str, object]] | None = None,
) -> str:
    if text and not tool_results:
        content: str | list[dict[str, object]] = text
    else:
        blocks: list[dict[str, object]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        if tool_results:
            for tr in tool_results:
                blocks.append({"type": "tool_result", **tr})
        content = blocks
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "timestamp": timestamp,
            "message": {"role": "user", "content": content},
        }
    )


class ScriptRunner:
    """Helper to run common_transcript.sh in a test environment."""

    def __init__(self, tmp_path: Path, stub_mng_log_sh: str) -> None:
        self.tmp_path = tmp_path
        self.agent_state_dir = tmp_path / "agent_state"

        # Create directory structure
        self.agent_state_dir.mkdir(parents=True)
        (self.agent_state_dir / "commands").mkdir(parents=True)
        (self.agent_state_dir / "logs" / "claude_transcript").mkdir(parents=True)

        # Write stub mng_log.sh
        log_path = self.agent_state_dir / "commands" / "mng_log.sh"
        log_path.write_text(stub_mng_log_sh)
        log_path.chmod(0o755)

        # Standard paths
        self.script_path = Path(__file__).parent / "common_transcript.sh"
        self.input_file = self.agent_state_dir / "logs" / "claude_transcript" / "events.jsonl"
        self.output_file = self.agent_state_dir / "events" / "claude" / "common_transcript" / "events.jsonl"

    def write_input(self, lines: list[str]) -> None:
        """Write lines to the input transcript file."""
        self.input_file.write_text("\n".join(lines) + "\n" if lines else "")

    def append_input(self, lines: list[str]) -> None:
        """Append lines to the input transcript file."""
        with self.input_file.open("a") as f:
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
            "MNG_AGENT_STATE_DIR": str(self.agent_state_dir),
        }
        return subprocess.run(
            ["bash", str(self.script_path), "--single-pass"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )


# -- Tests --


def test_no_input_file_produces_no_output(tmp_path: Path, stub_mng_log_sh: str) -> None:
    """With no input file, the script should produce no output."""
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_events() == []


def test_empty_input_file_produces_no_output(tmp_path: Path, stub_mng_log_sh: str) -> None:
    """An empty input file should produce no output."""
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    runner.write_input([])
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_events() == []


def test_converts_user_text_message(tmp_path: Path, stub_mng_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    runner.write_input([_make_user_event("uuid-1", "2026-01-01T00:00:00Z", text="Hello")])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "Hello"
    assert events[0]["event_id"] == "uuid-1-user"
    assert events[0]["source"] == "common_transcript"


def test_converts_assistant_message(tmp_path: Path, stub_mng_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    runner.write_input([_make_assistant_event("uuid-2", "2026-01-01T00:00:01Z", text="Hi there!")])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["type"] == "assistant_message"
    assert events[0]["text"] == "Hi there!"
    assert events[0]["model"] == "claude-opus-4.6"
    assert events[0]["event_id"] == "uuid-2-assistant"
    assert events[0]["stop_reason"] == "end_turn"
    assert events[0]["usage"]["input_tokens"] == 100


def test_converts_tool_calls(tmp_path: Path, stub_mng_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    runner.write_input(
        [
            _make_assistant_event(
                "uuid-3",
                "2026-01-01T00:00:02Z",
                tool_calls=[{"id": "toolu_1", "name": "Read", "input": {"file": "test.txt"}}],
                stop_reason="tool_use",
            )
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert len(events[0]["tool_calls"]) == 1
    assert events[0]["tool_calls"][0]["tool_name"] == "Read"
    assert events[0]["tool_calls"][0]["tool_call_id"] == "toolu_1"


def test_converts_tool_results(tmp_path: Path, stub_mng_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    assistant = _make_assistant_event(
        "uuid-4",
        "2026-01-01T00:00:03Z",
        tool_calls=[{"id": "toolu_2", "name": "Bash"}],
        stop_reason="tool_use",
    )
    user = _make_user_event(
        "uuid-5",
        "2026-01-01T00:00:04Z",
        tool_results=[{"tool_use_id": "toolu_2", "content": "output text", "is_error": False}],
    )
    runner.write_input([assistant, user])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 2
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["tool_call_id"] == "toolu_2"
    assert tool_results[0]["tool_name"] == "Bash"
    assert tool_results[0]["output"] == "output text"
    assert tool_results[0]["is_error"] is False


def test_deduplicates_by_event_id(tmp_path: Path, stub_mng_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    runner.write_input([_make_user_event("uuid-1", "2026-01-01T00:00:00Z", text="Hello")])

    # Pre-populate output with the same event_id
    runner.output_file.parent.mkdir(parents=True, exist_ok=True)
    runner.output_file.write_text(
        json.dumps({"event_id": "uuid-1-user", "type": "user_message", "content": "Hello"}) + "\n"
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    # Should not add a duplicate
    events = runner.get_output_events()
    assert len(events) == 1


def test_skips_progress_events(tmp_path: Path, stub_mng_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    progress = json.dumps(
        {
            "type": "progress",
            "uuid": "prog-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "data": {"type": "bash_progress"},
        }
    )
    runner.write_input([progress])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_events() == []


def test_handles_malformed_json(tmp_path: Path, stub_mng_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    valid = _make_user_event("uuid-1", "2026-01-01T00:00:00Z", text="valid")
    runner.write_input(["not json", valid])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["content"] == "valid"


def test_skips_events_without_uuid(tmp_path: Path, stub_mng_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    no_uuid = json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": "hi"}})
    runner.write_input([no_uuid])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_events() == []


def test_user_with_text_and_tool_results(tmp_path: Path, stub_mng_log_sh: str) -> None:
    """A user message with both text and tool results should emit both."""
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    assistant = _make_assistant_event(
        "uuid-a",
        "2026-01-01T00:00:01Z",
        tool_calls=[{"id": "toolu_3", "name": "Edit"}],
        stop_reason="tool_use",
    )
    user = json.dumps(
        {
            "type": "user",
            "uuid": "uuid-u",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Continue please"},
                    {"type": "tool_result", "tool_use_id": "toolu_3", "content": "done", "is_error": False},
                ],
            },
        }
    )
    runner.write_input([assistant, user])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    types_found = [e["type"] for e in events]
    assert "assistant_message" in types_found
    assert "user_message" in types_found
    assert "tool_result" in types_found


def test_truncates_tool_input_preview(tmp_path: Path, stub_mng_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    long_input = {"file": "x" * 500}
    runner.write_input(
        [
            _make_assistant_event(
                "uuid-long",
                "2026-01-01T00:00:00Z",
                tool_calls=[{"id": "toolu_long", "name": "Read", "input": long_input}],
            )
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    input_preview = events[0]["tool_calls"][0]["input_preview"]
    assert len(input_preview) <= 203


def test_truncates_long_tool_output(tmp_path: Path, stub_mng_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    assistant = _make_assistant_event(
        "uuid-tr",
        "2026-01-01T00:00:01Z",
        tool_calls=[{"id": "toolu_tr", "name": "Read"}],
        stop_reason="tool_use",
    )
    long_output = "x" * 5000
    user = _make_user_event(
        "uuid-tr2",
        "2026-01-01T00:00:02Z",
        tool_results=[{"tool_use_id": "toolu_tr", "content": long_output, "is_error": False}],
    )
    runner.write_input([assistant, user])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_results[0]["output"]) <= 2003


def test_tool_result_with_list_content(tmp_path: Path, stub_mng_log_sh: str) -> None:
    """Tool result content can be a list of text blocks."""
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    assistant = _make_assistant_event(
        "uuid-lc",
        "2026-01-01T00:00:01Z",
        tool_calls=[{"id": "toolu_lc", "name": "Read"}],
        stop_reason="tool_use",
    )
    user = json.dumps(
        {
            "type": "user",
            "uuid": "uuid-lc2",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_lc",
                        "content": [{"type": "text", "text": "part 1"}, {"type": "text", "text": "part 2"}],
                        "is_error": False,
                    }
                ],
            },
        }
    )
    runner.write_input([assistant, user])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert tool_results[0]["output"] == "part 1\npart 2"


def test_sorts_by_timestamp(tmp_path: Path, stub_mng_log_sh: str) -> None:
    """Events should be output sorted by timestamp."""
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    later = _make_user_event("uuid-later", "2026-01-01T00:00:02Z", text="Later")
    earlier = _make_user_event("uuid-earlier", "2026-01-01T00:00:01Z", text="Earlier")
    runner.write_input([later, earlier])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 2
    assert events[0]["content"] == "Earlier"
    assert events[1]["content"] == "Later"


def test_cache_read_and_write_tokens(tmp_path: Path, stub_mng_log_sh: str) -> None:
    """Verify cache_read and cache_write tokens are captured from usage."""
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    runner.write_input(
        [
            _make_assistant_event(
                "uuid-cache",
                "2026-01-01T00:00:00Z",
                text="Hello",
                usage={
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 80,
                    "cache_creation_input_tokens": 20,
                },
            )
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    usage = events[0]["usage"]
    assert usage["cache_read_tokens"] == 80
    assert usage["cache_write_tokens"] == 20


def test_unknown_tool_name_defaults(tmp_path: Path, stub_mng_log_sh: str) -> None:
    """Tool results for unknown tool_call_ids should get tool_name='unknown'."""
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    user = _make_user_event(
        "uuid-unk",
        "2026-01-01T00:00:01Z",
        tool_results=[{"tool_use_id": "toolu_unknown", "content": "result", "is_error": False}],
    )
    runner.write_input([user])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert tool_results[0]["tool_name"] == "unknown"


def test_output_writes_to_correct_path(tmp_path: Path, stub_mng_log_sh: str) -> None:
    """Output should go to events/claude/common_transcript/events.jsonl."""
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    runner.write_input([_make_user_event("uuid-1", "2026-01-01T00:00:00Z", text="Hello")])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    expected_path = runner.agent_state_dir / "events" / "claude" / "common_transcript" / "events.jsonl"
    assert expected_path.exists()
    assert len(expected_path.read_text().strip().splitlines()) == 1


def test_incremental_conversion(tmp_path: Path, stub_mng_log_sh: str) -> None:
    """Running twice with new input should append without duplicates."""
    runner = ScriptRunner(tmp_path, stub_mng_log_sh)
    runner.write_input([_make_user_event("uuid-1", "2026-01-01T00:00:00Z", text="First")])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert len(runner.get_output_events()) == 1

    # Append a new event to input
    runner.append_input([_make_user_event("uuid-2", "2026-01-01T00:00:01Z", text="Second")])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 2
    assert events[0]["content"] == "First"
    assert events[1]["content"] == "Second"
