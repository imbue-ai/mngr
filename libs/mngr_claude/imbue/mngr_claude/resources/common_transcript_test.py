"""Tests for common_transcript.sh.

Exercises the script's core behaviors by running it with --single-pass in a
controlled filesystem layout. Each test sets up:
  - A fake agent state dir with a raw claude transcript input file
  - A stub mngr_log.sh (no-op logging)

The --single-pass flag makes the script run one conversion pass then exit,
so tests are fast and deterministic.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from imbue.mngr import resources as mngr_resources
from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record

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
    """Build a real (human-typed) user event."""
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


def _make_meta_event(uuid: str, timestamp: str, text: str) -> str:
    """Build a framework-injected event (isMeta=true), e.g. Claude Code stop hook output.

    Claude Code emits these with type='user' and isMeta=true on the top-level
    JSONL entry. They are not human input despite the user type.
    """
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "timestamp": timestamp,
            "isMeta": True,
            "message": {"role": "user", "content": text},
        }
    )


class ScriptRunner:
    """Helper to run common_transcript.sh in a test environment."""

    def __init__(self, tmp_path: Path, stub_mngr_log_sh: str) -> None:
        self.tmp_path = tmp_path
        self.agent_state_dir = tmp_path / "agent_state"

        # Create directory structure
        self.agent_state_dir.mkdir(parents=True)
        (self.agent_state_dir / "commands").mkdir(parents=True)
        (self.agent_state_dir / "logs" / "claude_transcript").mkdir(parents=True)

        # Write stub mngr_log.sh
        log_path = self.agent_state_dir / "commands" / "mngr_log.sh"
        log_path.write_text(stub_mngr_log_sh)
        log_path.chmod(0o755)

        # Write the real shared common-transcript lib: the converter sources it
        # for the convert lock, mirroring Host._ensure_shared_shell_libs.
        lib_path = self.agent_state_dir / "commands" / "mngr_common_transcript_lib.sh"
        lib_path.write_text(
            importlib.resources.files(mngr_resources).joinpath("mngr_common_transcript_lib.sh").read_text()
        )
        lib_path.chmod(0o755)

        # Standard paths
        self.script_path = Path(__file__).parent / "common_transcript.sh"
        self.input_file = self.agent_state_dir / "logs" / "claude_transcript" / "events.jsonl"
        self.output_file = self.agent_state_dir / "events" / "claude" / "common_transcript" / "events.jsonl"
        # The mkdir-based mutex the converter takes around its read-modify-write.
        self.lock_dir = self.agent_state_dir / ".common_transcript_convert.lock"

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

    def run_single_pass(
        self, timeout: float = 10.0, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run the script with --single-pass."""
        env = {
            **os.environ,
            "MNGR_AGENT_STATE_DIR": str(self.agent_state_dir),
            **(extra_env or {}),
        }
        return subprocess.run(
            ["bash", str(self.script_path), "--single-pass"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )


# -- Tests --


def test_no_input_file_produces_no_output(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """With no input file, the script should produce no output."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_events() == []


def test_empty_input_file_produces_no_output(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """An empty input file should produce no output."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input([])
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_events() == []


def test_converts_user_text_message(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    user_uuid = uuid4().hex
    runner.write_input([_make_user_event(user_uuid, "2026-01-01T00:00:00Z", text="Hello")])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "Hello"
    assert events[0]["event_id"] == f"{user_uuid}-user"
    assert events[0]["source"] == "claude/common_transcript"


def test_converts_assistant_message(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    assistant_uuid = uuid4().hex
    runner.write_input([_make_assistant_event(assistant_uuid, "2026-01-01T00:00:01Z", text="Hi there!")])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["type"] == "assistant_message"
    assert events[0]["text"] == "Hi there!"
    assert events[0]["model"] == "claude-opus-4.6"
    assert events[0]["event_id"] == f"{assistant_uuid}-assistant"
    assert events[0]["finish_reason"] == "end_turn"
    assert events[0]["usage"]["input_tokens"] == 100


def test_converts_tool_calls(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    tool_call_id = f"toolu_{uuid4().hex}"
    runner.write_input(
        [
            _make_assistant_event(
                uuid4().hex,
                "2026-01-01T00:00:02Z",
                tool_calls=[{"id": tool_call_id, "name": "Read", "input": {"file": "test.txt"}}],
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
    assert events[0]["tool_calls"][0]["tool_call_id"] == tool_call_id


def test_converts_tool_results(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    tool_call_id = f"toolu_{uuid4().hex}"
    assistant = _make_assistant_event(
        uuid4().hex,
        "2026-01-01T00:00:03Z",
        tool_calls=[{"id": tool_call_id, "name": "Bash"}],
        stop_reason="tool_use",
    )
    user = _make_user_event(
        uuid4().hex,
        "2026-01-01T00:00:04Z",
        tool_results=[{"tool_use_id": tool_call_id, "content": "output text", "is_error": False}],
    )
    runner.write_input([assistant, user])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 2
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["tool_call_id"] == tool_call_id
    assert tool_results[0]["tool_name"] == "Bash"
    assert tool_results[0]["output"] == "output text"
    assert tool_results[0]["is_error"] is False


def test_deduplicates_by_event_id(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    user_uuid = uuid4().hex
    runner.write_input([_make_user_event(user_uuid, "2026-01-01T00:00:00Z", text="Hello")])

    # Pre-populate output with the same event_id
    runner.output_file.parent.mkdir(parents=True, exist_ok=True)
    runner.output_file.write_text(
        json.dumps({"event_id": f"{user_uuid}-user", "type": "user_message", "content": "Hello"}) + "\n"
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    # Should not add a duplicate
    events = runner.get_output_events()
    assert len(events) == 1


def test_skips_progress_events(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """A progress event is dropped while a sibling user message in the same input survives."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    progress = json.dumps(
        {
            "type": "progress",
            "uuid": uuid4().hex,
            "timestamp": "2026-01-01T00:00:00Z",
            "data": {"type": "bash_progress"},
        }
    )
    good_uuid = uuid4().hex
    good = _make_user_event(good_uuid, "2026-01-01T00:00:01Z", text="kept")
    runner.write_input([progress, good])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "kept"
    assert events[0]["event_id"] == f"{good_uuid}-user"


def test_handles_malformed_json(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    valid = _make_user_event(uuid4().hex, "2026-01-01T00:00:00Z", text="valid")
    runner.write_input(["not json", valid])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["content"] == "valid"


def test_missing_output_file_emits_nothing_to_pane(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """On the first pass the output file does not exist yet; the watcher must
    stay completely silent on stdout/stderr while still converting the event.
    The converter's count is captured by the shell, never echoed to the pane.
    """
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input([_make_user_event(uuid4().hex, "2026-01-01T00:00:00Z", text="Hello")])
    assert not runner.output_file.exists()

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert result.stdout == "", f"unexpected stdout: {result.stdout!r}"
    assert result.stderr == "", f"unexpected stderr: {result.stderr!r}"
    # The conversion still happens; only the pane noise is gone.
    assert len(runner.get_output_events()) == 1


def test_dropped_lines_emit_nothing_to_pane(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Malformed and null-message lines are dropped silently and must produce no
    output on the watcher's stdout/stderr; the valid line still converts.
    """
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    null_message = json.dumps(
        {"type": "user", "uuid": uuid4().hex, "timestamp": "2026-01-01T00:00:00Z", "message": None}
    )
    valid = _make_user_event(uuid4().hex, "2026-01-01T00:00:01Z", text="kept")
    runner.write_input(["not json", null_message, valid])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert result.stdout == "", f"unexpected stdout: {result.stdout!r}"
    assert result.stderr == "", f"unexpected stderr: {result.stderr!r}"
    # The bad lines are dropped; the valid one still converts.
    events = runner.get_output_events()
    assert [e["content"] for e in events] == ["kept"]


def test_skips_events_without_uuid(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """An event missing uuid is dropped while a sibling valid event survives."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    no_uuid = json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": "hi"}})
    good_uuid = uuid4().hex
    good = _make_user_event(good_uuid, "2026-01-01T00:00:01Z", text="kept")
    runner.write_input([no_uuid, good])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "kept"
    assert events[0]["event_id"] == f"{good_uuid}-user"


def test_skips_events_without_timestamp(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """An event missing timestamp is dropped while a sibling valid event survives.

    This exercises the timestamp branch of the `if not uuid or not timestamp` guard.
    """
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    no_timestamp = json.dumps({"type": "user", "uuid": uuid4().hex, "message": {"content": "hi"}})
    good_uuid = uuid4().hex
    good = _make_user_event(good_uuid, "2026-01-01T00:00:01Z", text="kept")
    runner.write_input([no_timestamp, good])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "kept"
    assert events[0]["event_id"] == f"{good_uuid}-user"


def test_user_with_text_and_tool_results(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """A user message with both text and tool results should emit both."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    tool_call_id = f"toolu_{uuid4().hex}"
    assistant = _make_assistant_event(
        uuid4().hex,
        "2026-01-01T00:00:01Z",
        tool_calls=[{"id": tool_call_id, "name": "Edit"}],
        stop_reason="tool_use",
    )
    user = json.dumps(
        {
            "type": "user",
            "uuid": uuid4().hex,
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Continue please"},
                    {"type": "tool_result", "tool_use_id": tool_call_id, "content": "done", "is_error": False},
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


def test_truncates_tool_input_preview(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    long_input = {"file": "x" * 500}
    runner.write_input(
        [
            _make_assistant_event(
                uuid4().hex,
                "2026-01-01T00:00:00Z",
                tool_calls=[{"id": f"toolu_{uuid4().hex}", "name": "Read", "input": long_input}],
            )
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    input_preview = events[0]["tool_calls"][0]["input_preview"]
    # Production truncates the compact JSON serialization to exactly _MAX_INPUT_PREVIEW_LENGTH
    # (200) chars and appends "..." -> 203 chars total.
    expected_full = json.dumps(long_input, separators=(",", ":"))
    assert len(input_preview) == 203
    assert input_preview.endswith("...")
    assert input_preview[:200] == expected_full[:200]


def test_does_not_truncate_short_tool_input_preview(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """A short tool input is emitted verbatim with no truncation marker."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    short_input = {"file": "test.txt"}
    runner.write_input(
        [
            _make_assistant_event(
                uuid4().hex,
                "2026-01-01T00:00:00Z",
                tool_calls=[{"id": f"toolu_{uuid4().hex}", "name": "Read", "input": short_input}],
            )
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    input_preview = events[0]["tool_calls"][0]["input_preview"]
    assert input_preview == json.dumps(short_input, separators=(",", ":"))
    assert not input_preview.endswith("...")


def test_truncates_long_tool_output(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    tool_call_id = f"toolu_{uuid4().hex}"
    assistant = _make_assistant_event(
        uuid4().hex,
        "2026-01-01T00:00:01Z",
        tool_calls=[{"id": tool_call_id, "name": "Read"}],
        stop_reason="tool_use",
    )
    long_output = "x" * 5000
    user = _make_user_event(
        uuid4().hex,
        "2026-01-01T00:00:02Z",
        tool_results=[{"tool_use_id": tool_call_id, "content": long_output, "is_error": False}],
    )
    runner.write_input([assistant, user])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    tool_results = [e for e in events if e["type"] == "tool_result"]
    output = tool_results[0]["output"]
    # Production truncates to exactly _MAX_OUTPUT_LENGTH (2000) chars plus "..." -> 2003.
    assert len(output) == 2003
    assert output.endswith("...")
    assert output[:2000] == long_output[:2000]


def test_does_not_truncate_short_tool_output(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """A short tool output is emitted verbatim with no truncation marker."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    tool_call_id = f"toolu_{uuid4().hex}"
    assistant = _make_assistant_event(
        uuid4().hex,
        "2026-01-01T00:00:01Z",
        tool_calls=[{"id": tool_call_id, "name": "Read"}],
        stop_reason="tool_use",
    )
    short_output = "all good"
    user = _make_user_event(
        uuid4().hex,
        "2026-01-01T00:00:02Z",
        tool_results=[{"tool_use_id": tool_call_id, "content": short_output, "is_error": False}],
    )
    runner.write_input([assistant, user])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert tool_results[0]["output"] == short_output
    assert not tool_results[0]["output"].endswith("...")


def test_tool_result_with_list_content(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Tool result content can be a list of text blocks."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    tool_call_id = f"toolu_{uuid4().hex}"
    assistant = _make_assistant_event(
        uuid4().hex,
        "2026-01-01T00:00:01Z",
        tool_calls=[{"id": tool_call_id, "name": "Read"}],
        stop_reason="tool_use",
    )
    user = json.dumps(
        {
            "type": "user",
            "uuid": uuid4().hex,
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
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


def test_sorts_by_timestamp(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Events should be output sorted by timestamp."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    later = _make_user_event(uuid4().hex, "2026-01-01T00:00:02Z", text="Later")
    earlier = _make_user_event(uuid4().hex, "2026-01-01T00:00:01Z", text="Earlier")
    runner.write_input([later, earlier])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 2
    assert events[0]["content"] == "Earlier"
    assert events[1]["content"] == "Later"


def test_cache_read_and_write_tokens(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Verify cache_read and cache_write tokens are captured from usage."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input(
        [
            _make_assistant_event(
                uuid4().hex,
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


def test_unknown_tool_name_defaults(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Tool results for unknown tool_call_ids should get tool_name='unknown'."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    user = _make_user_event(
        uuid4().hex,
        "2026-01-01T00:00:01Z",
        tool_results=[{"tool_use_id": f"toolu_{uuid4().hex}", "content": "result", "is_error": False}],
    )
    runner.write_input([user])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert tool_results[0]["tool_name"] == "unknown"


def test_output_writes_to_correct_path(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Output should go to events/claude/common_transcript/events.jsonl."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input([_make_user_event(uuid4().hex, "2026-01-01T00:00:00Z", text="Hello")])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    expected_path = runner.agent_state_dir / "events" / "claude" / "common_transcript" / "events.jsonl"
    assert expected_path.exists()
    assert len(expected_path.read_text().strip().splitlines()) == 1


def test_meta_user_message_classified_as_meta(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """isMeta=true messages (stop hook output, local-command caveats, etc.) get tool_name='meta'.

    Claude Code injects framework-generated content into the user-message stream with
    isMeta=true. Transcripts should show all such content under the tool role since no
    human typed it.
    """
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    meta_uuid = uuid4().hex
    feedback = (
        "Stop hook feedback:\n[./scripts/main_claude_stop_hook.sh]: Everything up-to-date\nERROR: Some checks failed"
    )
    runner.write_input([_make_meta_event(meta_uuid, "2026-01-01T00:00:00Z", text=feedback)])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["type"] == "tool_result"
    assert events[0]["tool_name"] == "meta"
    assert events[0]["tool_call_id"] == f"meta-{meta_uuid}"
    assert events[0]["output"] == feedback
    assert events[0]["is_error"] is False
    assert events[0]["event_id"] == f"{meta_uuid}-meta"


def test_meta_user_message_truncates_long_output(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    feedback = "Stop hook feedback:\n" + "x" * 5000
    runner.write_input([_make_meta_event(uuid4().hex, "2026-01-01T00:00:00Z", text=feedback)])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["type"] == "tool_result"
    output = events[0]["output"]
    # Production truncates to exactly _MAX_OUTPUT_LENGTH (2000) chars plus "..." -> 2003.
    assert len(output) == 2003
    assert output.endswith("...")
    assert output[:2000] == feedback[:2000]


def test_meta_user_message_does_not_truncate_short_output(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """A short meta message is emitted verbatim with no truncation marker."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    feedback = "Stop hook feedback:\nEverything up-to-date"
    runner.write_input([_make_meta_event(uuid4().hex, "2026-01-01T00:00:00Z", text=feedback)])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["type"] == "tool_result"
    assert events[0]["output"] == feedback
    assert not events[0]["output"].endswith("...")


def test_meta_user_message_with_list_content(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """isMeta=true messages delivered as a content list (with a text block) are also reclassified."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    user = json.dumps(
        {
            "type": "user",
            "uuid": uuid4().hex,
            "timestamp": "2026-01-01T00:00:00Z",
            "isMeta": True,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Stop hook feedback:\nWARN: nothing"}],
            },
        }
    )
    runner.write_input([user])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["type"] == "tool_result"
    assert events[0]["tool_name"] == "meta"


def test_real_claude_stop_hook_entry_classified_correctly(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Regression test pinned to a real Claude Code stop hook session entry.

    If Claude Code drops the isMeta flag from these injected entries, this test
    fails loudly so the converter can be updated deliberately. The fixture below
    was captured from an actual ~/.claude/projects/.../*.jsonl line emitted by
    Claude Code; only the uuid and timestamp were sanitized.
    """
    real_entry = (
        '{"type": "user", "uuid": "fixture-uuid", "timestamp": "2026-01-01T00:00:00.000Z",'
        ' "isMeta": true, "message": {"role": "user", "content":'
        ' "Stop hook feedback:\\n[${CLAUDE_PLUGIN_ROOT}/scripts/stop_hook_orchestrator.sh]:'
        " Everything up-to-date\\nThe following review gates have not been satisfied:\\n"
        '  - architecture verification (/verify-architecture)"}}'
    )
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input([real_entry])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["type"] == "tool_result"
    assert events[0]["tool_name"] == "meta"


def test_user_text_quoting_stop_hook_marker_without_is_meta_stays_user(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """A real user message quoting the stop hook marker (no isMeta) is NOT reclassified.

    This is the discriminating case where a content-prefix-only check would misfire:
    a human pasting the marker into chat must still appear under the user role.
    """
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input(
        [
            _make_user_event(
                uuid4().hex,
                "2026-01-01T00:00:00Z",
                text="Stop hook feedback:\nplease explain what this means",
            )
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 1
    assert events[0]["type"] == "user_message"


def test_emitted_common_records_conform_to_canonical_schema(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Every record claude's converter emits must validate against the shared envelope schema.

    Guards against the claude emitter (common_transcript.sh) and the canonical schema
    (imbue.mngr.agents.common_transcript_records) drifting apart. Drives all three record
    types and asserts each emitted record conforms.
    """
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    assistant = _make_assistant_event(
        "uuid-assistant",
        "2026-01-01T00:00:01Z",
        text="hi there",
        tool_calls=[{"id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}],
        stop_reason="tool_use",
    )
    user = _make_user_event(
        "uuid-user",
        "2026-01-01T00:00:02Z",
        text="hello",
        tool_results=[{"tool_use_id": "toolu_1", "content": "file.txt", "is_error": False}],
    )
    runner.write_input([assistant, user])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    records = runner.get_output_events()
    assert {r["type"] for r in records} == {"user_message", "assistant_message", "tool_result"}
    for record in records:
        assert validate_common_transcript_record(record) is None, record


def test_incremental_conversion(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Running twice with new input should append without duplicates."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input([_make_user_event(uuid4().hex, "2026-01-01T00:00:00Z", text="First")])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert len(runner.get_output_events()) == 1

    # Append a new event to input
    runner.append_input([_make_user_event(uuid4().hex, "2026-01-01T00:00:01Z", text="Second")])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    events = runner.get_output_events()
    assert len(events) == 2
    assert events[0]["content"] == "First"
    assert events[1]["content"] == "Second"


def test_held_lock_skips_pass(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """A pass that cannot take the convert lock (held by a concurrent pass)
    skips its conversion rather than racing into duplicate output. Simulated by
    pre-creating the (fresh) lock dir and giving the pass a short lock timeout."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input([_make_assistant_event(uuid4().hex, "2026-01-01T00:00:01Z", text="hi")])

    # Hold the lock with a fresh mtime so the stale-break (>1min) does not fire.
    runner.lock_dir.mkdir(parents=True)

    result = runner.run_single_pass(extra_env={"MNGR_CONVERT_LOCK_TIMEOUT": "1"})
    assert result.returncode == 0, f"stderr: {result.stderr}"
    # Lock was held the whole time, so nothing was converted.
    assert runner.get_output_events() == []

    # Release the lock; the next pass converts normally.
    runner.lock_dir.rmdir()
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert len(runner.get_output_events()) == 1


def test_stale_lock_is_broken(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """A convert lock older than a minute is treated as stale (left by a crashed
    pass) and broken, so the converter never wedges permanently."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input([_make_assistant_event(uuid4().hex, "2026-01-01T00:00:01Z", text="hi")])

    runner.lock_dir.mkdir(parents=True)
    # Age the lock past the 1-minute stale threshold.
    stale = time.time() - 120
    os.utime(runner.lock_dir, (stale, stale))

    result = runner.run_single_pass(extra_env={"MNGR_CONVERT_LOCK_TIMEOUT": "1"})
    assert result.returncode == 0, f"stderr: {result.stderr}"
    # The stale lock was broken, so conversion proceeded.
    assert len(runner.get_output_events()) == 1


def test_concurrent_passes_do_not_duplicate(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Two passes racing over the same input must not both append the same
    events: the lock serializes them so the second sees the first's output in
    its dedup set. Without the lock this produces duplicate event_ids."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input(
        [_make_assistant_event(uuid4().hex, f"2026-01-01T00:00:{i:02d}Z", text=f"m{i}") for i in range(20)]
    )

    env = {**os.environ, "MNGR_AGENT_STATE_DIR": str(runner.agent_state_dir)}
    procs = [
        subprocess.Popen(
            ["bash", str(runner.script_path), "--single-pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        for _ in range(2)
    ]
    for proc in procs:
        assert proc.wait(timeout=30) == 0

    events = runner.get_output_events()
    event_ids = [e["event_id"] for e in events]
    assert len(event_ids) == len(set(event_ids)), "convert lock failed to prevent duplicate events"
    assert len(events) == 20
