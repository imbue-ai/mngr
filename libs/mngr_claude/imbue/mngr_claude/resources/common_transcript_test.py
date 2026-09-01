"""Tests for common_transcript.sh.

Exercises the script's core behaviors by running it in a controlled filesystem
layout. Each test sets up:
  - A fake agent state dir with a raw claude transcript input file
  - A stub mngr_log.sh (no-op logging)

Most tests use --single-pass, which runs one conversion pass then exits, so they
are fast and deterministic. It is also the turn-end flush path, so it tells the
converter the input is complete (see ``_MNGR_EMIT_TRAILING_ASSISTANT_GROUP`` in
common_transcript.sh). The mid-turn form is driven two ways: by the converter's
environment alone (``ScriptRunner.run_converter_as_daemon``), and by the real
watcher's poll loop (``ScriptRunner.start_watcher``).

Behavior of the converter itself is unit-tested in
common_transcript_convert_test.py; the tests here cover what only the shell can
show -- the lock, pane silence, paths, incremental passes, the --single-pass
wiring, and schema conformance of the end-to-end output.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from imbue.mngr import resources as mngr_resources
from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_claude.resources import common_transcript_convert
from imbue.mngr_claude.resources.testing import DEFAULT_MODEL
from imbue.mngr_claude.resources.testing import DEFAULT_PROMPT_TOKENS
from imbue.mngr_claude.resources.testing import make_assistant_record
from imbue.mngr_claude.resources.testing import make_tool_result_record
from imbue.mngr_claude.resources.testing import make_user_record
from imbue.mngr_claude.resources.testing import read_observations
from imbue.mngr_claude.resources.testing import read_steps
from imbue.mngr_claude.resources.testing import read_stream
from imbue.mngr_claude.resources.testing import write_raw_transcript

# The watcher polls every 5s but converts immediately on startup, so the first
# pass lands well inside this budget; it is generous only to survive a loaded CI box.
_WATCHER_CONVERSION_TIMEOUT = 30.0
_WATCHER_POLL_INTERVAL = 0.2
# The watcher spends nearly all its time in `sleep`, which defers the SIGTERM until
# the current sleep returns.
_WATCHER_EXIT_TIMEOUT = 15.0

# -- Helpers --


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
        self.converter_path = Path(__file__).parent / "common_transcript_convert.py"
        self.input_file = self.agent_state_dir / "logs" / "claude_transcript" / "events.jsonl"
        self.output_file = self.agent_state_dir / "events" / "claude" / "common_transcript" / "events.jsonl"
        # The mkdir-based mutex the converter takes around its read-modify-write.
        self.lock_dir = self.agent_state_dir / ".common_transcript_convert.lock"

    def write_input(self, records: list[dict[str, Any] | str]) -> None:
        """Write raw transcript records (or verbatim strings) to the input file."""
        write_raw_transcript(self.input_file, records)

    def append_input(self, records: list[dict[str, Any] | str]) -> None:
        """Append raw transcript records to the input file."""
        with self.input_file.open("a") as f:
            for record in records:
                f.write((record if isinstance(record, str) else json.dumps(record)) + "\n")

    def get_output_events(self) -> list[dict[str, Any]]:
        return read_stream(self.output_file)

    def get_steps(self, source: str) -> list[dict[str, Any]]:
        return read_steps(self.output_file, source)

    def get_observations(self) -> list[dict[str, Any]]:
        return read_observations(self.output_file)

    def run_single_pass(
        self, timeout: float = 10.0, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run the script with --single-pass (the turn-end flush path)."""
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

    def run_converter_as_daemon(self, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
        """Run one conversion exactly as the script's poll loop does, mid-turn.

        The loop itself never exits, so the daemon's *environment* is reproduced
        instead: the converter invoked with the paths but without the flag that
        says the input is complete.
        """
        env = {
            **os.environ,
            "_INPUT_FILE": str(self.input_file),
            "_OUTPUT_FILE": str(self.output_file),
        }
        env.pop("_MNGR_EMIT_TRAILING_ASSISTANT_GROUP", None)
        return subprocess.run(
            ["python3", str(self.converter_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

    def start_watcher(self) -> subprocess.Popen[bytes]:
        """Start the real watcher (no --single-pass); its poll loop never exits.

        The caller owns the returned process and must kill it (by this exact
        handle, never by pattern).
        """
        env = {**os.environ, "MNGR_AGENT_STATE_DIR": str(self.agent_state_dir)}
        # Own process group so stop_watcher can kill the poll loop's in-flight
        # converter child along with the bash parent.
        return subprocess.Popen(
            ["bash", str(self.script_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )

    def stop_watcher(self, watcher: subprocess.Popen[bytes]) -> None:
        """Stop a watcher started by ``start_watcher`` (SIGTERM, then SIGKILL).

        Kills the whole process group: a SIGTERM to the bash parent alone can
        land while its converter child holds the convert lock, and bash dies
        without releasing it -- the next single-pass then waits out the 30s lock
        timeout instead of converting. With the group dead, any leftover lock
        dir is orphaned and safe to clear.
        """
        self._signal_watcher_group(watcher, signal.SIGTERM)
        try:
            watcher.wait(timeout=_WATCHER_EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._signal_watcher_group(watcher, signal.SIGKILL)
            watcher.wait(timeout=_WATCHER_EXIT_TIMEOUT)
        shutil.rmtree(self.lock_dir, ignore_errors=True)

    @staticmethod
    def _signal_watcher_group(watcher: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        # start_new_session makes the watcher its own group leader, so its pid
        # is the pgid. The group may already be gone.
        try:
            os.killpg(watcher.pid, sig)
        except ProcessLookupError:
            pass


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


def test_first_line_is_the_stream_header(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input([make_user_record(uuid4().hex, timestamp="2026-01-01T00:00:00Z", text="Hello")])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    # The script passes basename($MNGR_AGENT_STATE_DIR) as the agent id.
    assert runner.get_output_events()[0] == {
        "type": "header",
        "event_id": common_transcript_convert._header_event_id("agent_state"),
        "emitter": "claude/common_transcript",
        "schema_version": "ATIF-v1.7",
    }


def test_converts_user_text_message(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    user_uuid = uuid4().hex
    runner.write_input([make_user_record(user_uuid, timestamp="2026-01-01T00:00:00Z", text="Hello")])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    steps = runner.get_steps("user")
    assert len(steps) == 1
    assert steps[0]["message"] == "Hello"
    assert steps[0]["event_id"] == f"{user_uuid}-user"
    assert steps[0]["emitter"] == "claude/common_transcript"


def test_converts_assistant_message(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    assistant_uuid = uuid4().hex
    runner.write_input([make_assistant_record(assistant_uuid, timestamp="2026-01-01T00:00:01Z", text="Hi there!")])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    steps = runner.get_steps("agent")
    assert len(steps) == 1
    assert steps[0]["message"] == "Hi there!"
    assert steps[0]["model_name"] == DEFAULT_MODEL
    assert steps[0]["event_id"] == f"{assistant_uuid}-assistant"
    assert steps[0]["extra"]["finish_reason"] == "end_turn"
    assert steps[0]["metrics"]["prompt_tokens"] == DEFAULT_PROMPT_TOKENS


def test_converts_tool_calls(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    tool_call_id = f"toolu_{uuid4().hex}"
    runner.write_input(
        [
            make_assistant_record(
                uuid4().hex,
                timestamp="2026-01-01T00:00:02Z",
                tool_uses=[{"id": tool_call_id, "name": "Read", "input": {"file": "test.txt"}}],
                stop_reason="tool_use",
            )
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    steps = runner.get_steps("agent")
    assert len(steps) == 1
    assert steps[0]["tool_calls"] == [
        {"tool_call_id": tool_call_id, "function_name": "Read", "arguments": {"file": "test.txt"}}
    ]


def test_converts_tool_results(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    tool_call_id = f"toolu_{uuid4().hex}"
    assistant = make_assistant_record(
        uuid4().hex,
        timestamp="2026-01-01T00:00:03Z",
        tool_uses=[{"id": tool_call_id, "name": "Bash"}],
        stop_reason="tool_use",
    )
    user = make_user_record(
        uuid4().hex,
        timestamp="2026-01-01T00:00:04Z",
        tool_results=[{"tool_use_id": tool_call_id, "content": "output text", "is_error": False}],
    )
    runner.write_input([assistant, user])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    observations = runner.get_observations()
    assert len(observations) == 1
    assert observations[0]["results"] == [
        {
            "source_call_id": tool_call_id,
            "content": "output text",
            "extra": {"is_error": False, "tool_name": "Bash"},
        }
    ]


def test_deduplicates_by_event_id(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    user_uuid = uuid4().hex
    runner.write_input([make_user_record(user_uuid, timestamp="2026-01-01T00:00:00Z", text="Hello")])

    # Pre-populate output with the same event_id (and the header, which is deduped
    # the same way).
    runner.output_file.parent.mkdir(parents=True, exist_ok=True)
    runner.output_file.write_text(
        json.dumps({"event_id": common_transcript_convert._header_event_id("agent_state"), "type": "header"})
        + "\n"
        + json.dumps({"event_id": f"{user_uuid}-user", "type": "step", "message": "Hello"})
        + "\n"
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    # Should not add a duplicate
    events = runner.get_output_events()
    assert len(events) == 2


def test_skips_progress_events(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """A progress event is dropped while a sibling user message in the same input survives."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    progress = {
        "type": "progress",
        "uuid": uuid4().hex,
        "timestamp": "2026-01-01T00:00:00Z",
        "data": {"type": "bash_progress"},
    }
    good_uuid = uuid4().hex
    good = make_user_record(good_uuid, timestamp="2026-01-01T00:00:01Z", text="kept")
    runner.write_input([progress, good])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    steps = runner.get_steps("user")
    assert len(steps) == 1
    assert steps[0]["message"] == "kept"
    assert steps[0]["event_id"] == f"{good_uuid}-user"


def test_handles_malformed_json(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    valid = make_user_record(uuid4().hex, timestamp="2026-01-01T00:00:00Z", text="valid")
    runner.write_input(["not json", valid])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    assert [s["message"] for s in runner.get_steps("user")] == ["valid"]


def test_missing_output_file_emits_nothing_to_pane(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """On the first pass the output file does not exist yet; the watcher must
    stay completely silent on stdout/stderr while still converting the event.
    The converter's count is captured by the shell, never echoed to the pane.
    """
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input([make_user_record(uuid4().hex, timestamp="2026-01-01T00:00:00Z", text="Hello")])
    assert not runner.output_file.exists()

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert result.stdout == "", f"unexpected stdout: {result.stdout!r}"
    assert result.stderr == "", f"unexpected stderr: {result.stderr!r}"
    # The conversion still happens; only the pane noise is gone.
    assert len(runner.get_steps("user")) == 1


def test_dropped_lines_emit_nothing_to_pane(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Malformed and null-message lines are dropped silently and must produce no
    output on the watcher's stdout/stderr; the valid line still converts.
    """
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    null_message = {"type": "user", "uuid": uuid4().hex, "timestamp": "2026-01-01T00:00:00Z", "message": None}
    valid = make_user_record(uuid4().hex, timestamp="2026-01-01T00:00:01Z", text="kept")
    runner.write_input(["not json", null_message, valid])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert result.stdout == "", f"unexpected stdout: {result.stdout!r}"
    assert result.stderr == "", f"unexpected stderr: {result.stderr!r}"
    # The bad lines are dropped; the valid one still converts.
    assert [s["message"] for s in runner.get_steps("user")] == ["kept"]


def test_skips_events_without_uuid(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """An event missing uuid is dropped while a sibling valid event survives."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    no_uuid = {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": "hi"}}
    good_uuid = uuid4().hex
    good = make_user_record(good_uuid, timestamp="2026-01-01T00:00:01Z", text="kept")
    runner.write_input([no_uuid, good])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    steps = runner.get_steps("user")
    assert len(steps) == 1
    assert steps[0]["event_id"] == f"{good_uuid}-user"


def test_skips_events_without_timestamp(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """An event missing timestamp is dropped while a sibling valid event survives.

    This exercises the timestamp branch of the `if not uuid or not timestamp` guard.
    """
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    no_timestamp = {"type": "user", "uuid": uuid4().hex, "message": {"content": "hi"}}
    good_uuid = uuid4().hex
    good = make_user_record(good_uuid, timestamp="2026-01-01T00:00:01Z", text="kept")
    runner.write_input([no_timestamp, good])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    steps = runner.get_steps("user")
    assert len(steps) == 1
    assert steps[0]["event_id"] == f"{good_uuid}-user"


def test_user_with_text_and_tool_results(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """A user message with both text and tool results should emit both."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    tool_call_id = f"toolu_{uuid4().hex}"
    assistant = make_assistant_record(
        uuid4().hex,
        timestamp="2026-01-01T00:00:01Z",
        tool_uses=[{"id": tool_call_id, "name": "Edit"}],
        stop_reason="tool_use",
    )
    user = make_user_record(
        uuid4().hex,
        timestamp="2026-01-01T00:00:02Z",
        text="Continue please",
        tool_results=[{"tool_use_id": tool_call_id, "content": "done", "is_error": False}],
    )
    runner.write_input([assistant, user])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    assert len(runner.get_steps("agent")) == 1
    assert [s["message"] for s in runner.get_steps("user")] == ["Continue please"]
    assert len(runner.get_observations()) == 1


def test_tool_result_with_list_content(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Tool result content can be a list of text blocks."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    tool_call_id = f"toolu_{uuid4().hex}"
    assistant = make_assistant_record(
        uuid4().hex,
        timestamp="2026-01-01T00:00:01Z",
        tool_uses=[{"id": tool_call_id, "name": "Read"}],
        stop_reason="tool_use",
    )
    user = make_tool_result_record(
        uuid4().hex,
        tool_call_id,
        [{"type": "text", "text": "part 1"}, {"type": "text", "text": "part 2"}],
        timestamp="2026-01-01T00:00:02Z",
    )
    runner.write_input([assistant, user])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    assert runner.get_observations()[0]["results"][0]["content"] == "part 1\npart 2"


def test_sorts_by_timestamp(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Events should be output sorted by timestamp."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    later = make_user_record(uuid4().hex, timestamp="2026-01-01T00:00:02Z", text="Later")
    earlier = make_user_record(uuid4().hex, timestamp="2026-01-01T00:00:01Z", text="Earlier")
    runner.write_input([later, earlier])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    assert [s["message"] for s in runner.get_steps("user")] == ["Earlier", "Later"]


def test_unknown_tool_name_defaults(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Tool results for unknown tool_call_ids should get tool_name='unknown'."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    user = make_user_record(
        uuid4().hex,
        timestamp="2026-01-01T00:00:01Z",
        tool_results=[{"tool_use_id": f"toolu_{uuid4().hex}", "content": "result", "is_error": False}],
    )
    runner.write_input([user])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    assert runner.get_observations()[0]["results"][0]["extra"]["tool_name"] == "unknown"


def test_output_writes_to_correct_path(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Output should go to events/claude/common_transcript/events.jsonl."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input([make_user_record(uuid4().hex, timestamp="2026-01-01T00:00:00Z", text="Hello")])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    expected_path = runner.agent_state_dir / "events" / "claude" / "common_transcript" / "events.jsonl"
    assert expected_path.exists()
    # The header plus the one user step.
    assert len(expected_path.read_text().strip().splitlines()) == 2


def test_meta_user_message_becomes_a_system_step(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """isMeta=true messages (stop hook output, local-command caveats) are system steps.

    Claude Code injects framework-generated content into the user-message stream with
    isMeta=true. No human typed it, so it must not appear as a user turn.
    """
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    meta_uuid = uuid4().hex
    feedback = (
        "Stop hook feedback:\n[./scripts/main_claude_stop_hook.sh]: Everything up-to-date\nERROR: Some checks failed"
    )
    runner.write_input([make_user_record(meta_uuid, timestamp="2026-01-01T00:00:00Z", is_meta=True, text=feedback)])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    steps = runner.get_steps("system")
    assert len(steps) == 1
    assert steps[0]["message"] == feedback
    assert steps[0]["event_id"] == f"{meta_uuid}-meta"
    assert runner.get_steps("user") == []


def test_meta_user_message_with_list_content(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """isMeta=true messages delivered as a content list (with a text block) are also system steps."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    user = {
        "type": "user",
        "uuid": uuid4().hex,
        "timestamp": "2026-01-01T00:00:00Z",
        "isMeta": True,
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "Stop hook feedback:\nWARN: nothing"}],
        },
    }
    runner.write_input([user])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    assert len(runner.get_steps("system")) == 1


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

    assert len(runner.get_steps("system")) == 1
    assert runner.get_steps("user") == []


def test_user_text_quoting_stop_hook_marker_without_is_meta_stays_user(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """A real user message quoting the stop hook marker (no isMeta) is NOT reclassified.

    This is the discriminating case where a content-prefix-only check would misfire:
    a human pasting the marker into chat must still appear under the user role.
    """
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input(
        [
            make_user_record(
                uuid4().hex,
                timestamp="2026-01-01T00:00:00Z",
                text="Stop hook feedback:\nplease explain what this means",
            )
        ]
    )

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    assert len(runner.get_steps("user")) == 1
    assert runner.get_steps("system") == []


def test_emitted_common_records_conform_to_canonical_schema(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Every record claude's converter emits must validate against the shared record schema.

    Guards against the claude emitter (common_transcript.sh) and the canonical schema
    (imbue.mngr.agents.common_transcript_records) drifting apart. Drives all three
    record types and asserts each emitted record conforms.
    """
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    assistant = make_assistant_record(
        "uuid-assistant",
        timestamp="2026-01-01T00:00:01Z",
        text="hi there",
        thinking="let me look",
        tool_uses=[{"id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}],
        stop_reason="tool_use",
    )
    user = make_user_record(
        "uuid-user",
        timestamp="2026-01-01T00:00:02Z",
        text="hello",
        tool_results=[{"tool_use_id": "toolu_1", "content": "file.txt", "is_error": False}],
    )
    meta = make_user_record(
        "uuid-meta", timestamp="2026-01-01T00:00:03Z", is_meta=True, text="Stop hook feedback:\nok"
    )
    runner.write_input([assistant, user, meta])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    records = runner.get_output_events()
    assert {r["type"] for r in records} == {"header", "step", "observation"}
    assert {r["source"] for r in records if r["type"] == "step"} == {"user", "agent", "system"}
    for record in records:
        assert validate_common_transcript_record(record) is None, record


def test_incremental_conversion(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Running twice with new input should append without duplicates."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input([make_user_record(uuid4().hex, timestamp="2026-01-01T00:00:00Z", text="First")])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert len(runner.get_steps("user")) == 1

    # Append a new event to input
    runner.append_input([make_user_record(uuid4().hex, timestamp="2026-01-01T00:00:01Z", text="Second")])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    assert [s["message"] for s in runner.get_steps("user")] == ["First", "Second"]
    assert [e["type"] for e in runner.get_output_events()].count("header") == 1


def test_daemon_pass_defers_the_open_trailing_inference(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Mid-turn the last inference is still being appended to, line by line.

    The poll loop must hold it back (emitting it early would freeze it half-written,
    since the output is deduped by event_id); the turn-end --single-pass flush knows
    the input is complete and emits it.
    """
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input(
        [
            make_user_record(uuid4().hex, timestamp="2026-01-01T00:00:00Z", text="go"),
            make_assistant_record(uuid4().hex, timestamp="2026-01-01T00:00:01Z", text="working"),
        ]
    )

    result = runner.run_converter_as_daemon()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert [s["message"] for s in runner.get_steps("user")] == ["go"]
    assert runner.get_steps("agent") == []

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert [s["message"] for s in runner.get_steps("agent")] == ["working"]

    # A later daemon pass re-reads the whole input; it must not duplicate the
    # inference the flush already emitted.
    result = runner.run_converter_as_daemon()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    events = runner.get_output_events()
    assert len({e["event_id"] for e in events}) == len(events)
    assert [s["message"] for s in runner.get_steps("agent")] == ["working"]


@pytest.mark.timeout(60)
def test_running_watcher_defers_the_open_trailing_inference(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """The real poll loop must hold back the inference claude is still writing.

    ``run_converter_as_daemon`` reproduces the daemon's environment; this drives the
    daemon itself, so the wiring between the poll loop and the converter's
    input-complete flag is covered by the script that actually runs on the host.
    """
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input(
        [
            make_user_record("u1", timestamp="2026-01-01T00:00:00Z", text="go"),
            make_assistant_record("u2", timestamp="2026-01-01T00:00:01Z", text="closed", message_id="msg_1"),
            make_user_record("u3", timestamp="2026-01-01T00:00:02Z", text="and again"),
            make_assistant_record("u4", timestamp="2026-01-01T00:00:03Z", text="still writing", message_id="msg_2"),
        ]
    )

    watcher = runner.start_watcher()
    try:
        converted = poll_until(
            condition=lambda: runner.get_steps("agent") != [],
            timeout=_WATCHER_CONVERSION_TIMEOUT,
            poll_interval=_WATCHER_POLL_INTERVAL,
        )
        assert converted, "the watcher never converted the closed records"
        assert [s["message"] for s in runner.get_steps("user")] == ["go", "and again"]
        # The last inference is still being appended to, so the poll loop holds it back.
        assert [s["message"] for s in runner.get_steps("agent")] == ["closed"]
    finally:
        runner.stop_watcher(watcher)

    # The turn-end flush knows the turn is over, so it emits the trailing inference.
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert [s["message"] for s in runner.get_steps("agent")] == ["closed", "still writing"]


def test_held_lock_skips_pass(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """A pass that cannot take the convert lock (held by a concurrent pass)
    skips its conversion rather than racing into duplicate output. Simulated by
    pre-creating the (fresh) lock dir and giving the pass a short lock timeout."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input([make_assistant_record(uuid4().hex, timestamp="2026-01-01T00:00:01Z", text="hi")])

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
    assert len(runner.get_steps("agent")) == 1


def test_stale_lock_is_broken(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """A convert lock older than a minute is treated as stale (left by a crashed
    pass) and broken, so the converter never wedges permanently."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input([make_assistant_record(uuid4().hex, timestamp="2026-01-01T00:00:01Z", text="hi")])

    runner.lock_dir.mkdir(parents=True)
    # Age the lock past the 1-minute stale threshold.
    stale = time.time() - 120
    os.utime(runner.lock_dir, (stale, stale))

    result = runner.run_single_pass(extra_env={"MNGR_CONVERT_LOCK_TIMEOUT": "1"})
    assert result.returncode == 0, f"stderr: {result.stderr}"
    # The stale lock was broken, so conversion proceeded.
    assert len(runner.get_steps("agent")) == 1


def test_concurrent_passes_do_not_duplicate(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Two passes racing over the same input must not both append the same
    events: the lock serializes them so the second sees the first's output in
    its dedup set. Without the lock this produces duplicate event_ids."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh)
    runner.write_input(
        [make_assistant_record(uuid4().hex, timestamp=f"2026-01-01T00:00:{i:02d}Z", text=f"m{i}") for i in range(20)]
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
    # Each assistant line is its own inference, behind the single stream header.
    assert len(events) == 21
