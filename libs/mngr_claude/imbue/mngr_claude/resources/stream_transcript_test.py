"""Tests for stream_transcript.sh.

Exercises the script's core behaviors by running it with --single-pass in a
controlled filesystem layout. Each test sets up:
  - A fake agent state dir with session history, session JSONL files, and a stub mngr_log.sh
  - A fake ~/.claude/projects/ directory (via HOME override)

The --single-pass flag makes the script run initialization + one poll cycle
then exit, so tests are fast and deterministic.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from imbue.mngr.utils.testing import get_short_random_string

# Helpers


def _make_jsonl_line(uuid: str | None = None) -> str:
    """Create a minimal Claude session JSONL line with a uuid field."""
    line_uuid = uuid if uuid is not None else uuid4().hex
    return json.dumps({"uuid": line_uuid, "type": "assistant", "timestamp": "2026-01-01T00:00:00Z"})


def _iso_z_timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _make_user_record_line(content: Any, timestamp_epoch: float | None) -> str:
    """Create a raw session line for a `user` record with the given message content."""
    record: dict[str, Any] = {
        "uuid": uuid4().hex,
        "type": "user",
        "message": {"role": "user", "content": content},
    }
    if timestamp_epoch is not None:
        record["timestamp"] = _iso_z_timestamp(timestamp_epoch)
    return json.dumps(record)


def _make_interrupt_sentinel_line(
    timestamp_epoch: float | None, sentinel_text: str = "[Request interrupted by user]"
) -> str:
    """Create a raw session line for the `user` record claude appends when a turn is interrupted."""
    return _make_user_record_line([{"type": "text", "text": sentinel_text}], timestamp_epoch)


def _make_assistant_record_line(timestamp_epoch: float) -> str:
    """Create a raw session line for an `assistant` record (a reply calling a tool)."""
    return json.dumps(
        {
            "uuid": uuid4().hex,
            "type": "assistant",
            "timestamp": _iso_z_timestamp(timestamp_epoch),
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-1", "name": "Bash"}]},
        }
    )


def _make_queue_operation_line(operation: str, timestamp_epoch: float) -> str:
    """Create a raw session line for a `queue-operation` record (Claude's prompt queue)."""
    return json.dumps(
        {"type": "queue-operation", "operation": operation, "timestamp": _iso_z_timestamp(timestamp_epoch)}
    )


def _write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n" if lines else "")


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text().splitlines() if line.strip()]


def _extract_uuids(lines: list[str]) -> list[str]:
    uuids = []
    for line in lines:
        try:
            uuids.append(json.loads(line)["uuid"])
        except (json.JSONDecodeError, KeyError):
            pass
    return uuids


class ScriptRunner:
    """Helper to run stream_transcript.sh in a test environment."""

    def __init__(self, tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str) -> None:
        self.tmp_path = tmp_path
        self.agent_state_dir = tmp_path / "agent_state"
        self.host_dir = tmp_path / "host"
        # Fake HOME so ~/.claude/projects/ resolves to our test dir
        self.fake_home = tmp_path / "fakehome"
        self.claude_projects_dir = self.fake_home / ".claude" / "projects"

        # Create directory structure
        self.agent_state_dir.mkdir(parents=True)
        self.host_dir.mkdir(parents=True)
        self.claude_projects_dir.mkdir(parents=True)
        (self.agent_state_dir / "commands").mkdir(parents=True)
        (self.agent_state_dir / "events" / "logs" / "stream_transcript").mkdir(parents=True)

        # Write stub mngr_log.sh
        log_path = self.agent_state_dir / "commands" / "mngr_log.sh"
        log_path.write_text(stub_mngr_log_sh)
        log_path.chmod(0o755)

        # Write the real mngr_transcript_lib.sh (the streamer sources it).
        lib_path = self.agent_state_dir / "commands" / "mngr_transcript_lib.sh"
        lib_path.write_text(mngr_transcript_lib_sh)
        lib_path.chmod(0o755)

        # Standard paths
        self.script_path = Path(__file__).parent / "stream_transcript.sh"
        self.history_file = self.agent_state_dir / "claude_session_id_history"
        self.output_file = self.agent_state_dir / "logs" / "claude_transcript" / "events.jsonl"
        self.offset_dir = self.agent_state_dir / "plugin" / "claude" / ".transcript_offsets"

    def add_session(self, session_id: str, lines: list[str]) -> Path:
        """Create a session JSONL file and add the session to the history."""
        project_hash = get_short_random_string()
        session_dir = self.claude_projects_dir / project_hash
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / f"{session_id}.jsonl"
        _write_lines(session_file, lines)

        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        with self.history_file.open("a") as f:
            f.write(f"{session_id} hook\n")

        return session_file

    def set_offset(self, session_id: str, offset: int) -> None:
        self.offset_dir.mkdir(parents=True, exist_ok=True)
        (self.offset_dir / session_id).write_text(str(offset))

    def get_offset(self, session_id: str) -> int:
        offset_file = self.offset_dir / session_id
        if offset_file.exists():
            return int(offset_file.read_text().strip())
        return 0

    def get_output_lines(self) -> list[str]:
        return _read_lines(self.output_file)

    def get_output_uuids(self) -> list[str]:
        return _extract_uuids(self.get_output_lines())

    def set_current_session(self, session_id: str) -> None:
        """Write the claude_session_id marker the SessionStart hook maintains."""
        (self.agent_state_dir / "claude_session_id").write_text(session_id + "\n")

    def create_active_marker(self, mtime_epoch: float) -> Path:
        """Create the agent's `active` marker with its mtime pinned to mtime_epoch."""
        marker = self.agent_state_dir / "active"
        marker.touch()
        os.utime(marker, (mtime_epoch, mtime_epoch))
        return marker

    def get_activity_event_lines(self) -> list[str]:
        return _read_lines(self.host_dir / "events" / "mngr" / "activity" / "events.jsonl")

    def run_single_pass(
        self, timeout: float = 10.0, path_prefix: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run the script with --single-pass (initialize + one cycle + exit)."""
        env = {
            **os.environ,
            "MNGR_AGENT_STATE_DIR": str(self.agent_state_dir),
            "MNGR_HOST_DIR": str(self.host_dir),
            "HOME": str(self.fake_home),
            "CLAUDE_CONFIG_DIR": str(self.fake_home / ".claude"),
        }
        if path_prefix is not None:
            env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
        result = subprocess.run(
            ["bash", str(self.script_path), "--single-pass"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return result


# Tests


def test_empty_history_produces_no_output(tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str) -> None:
    """With no history file, the script should produce no output."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_lines() == []


def test_single_session_emits_all_lines(tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str) -> None:
    """A single session with 3 lines should emit all 3 to the output."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    uuids = [uuid4().hex for _ in range(3)]
    lines = [_make_jsonl_line(u) for u in uuids]
    runner.add_session("sess-1", lines)

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    assert runner.get_output_uuids() == uuids
    assert runner.get_offset("sess-1") == 3


def test_multiple_sessions_emit_all(tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str) -> None:
    """Multiple sessions should all have their lines emitted."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    uuids_a = [uuid4().hex for _ in range(2)]
    uuids_b = [uuid4().hex for _ in range(2)]

    runner.add_session("sess-a", [_make_jsonl_line(u) for u in uuids_a])
    runner.add_session("sess-b", [_make_jsonl_line(u) for u in uuids_b])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    output_uuids = runner.get_output_uuids()
    assert set(output_uuids) == set(uuids_a + uuids_b)
    assert len(output_uuids) == 4


def test_offset_tracking_skips_already_emitted(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    """With a stored offset, only new lines should be emitted."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    uuids = [uuid4().hex for _ in range(5)]
    lines = [_make_jsonl_line(u) for u in uuids]
    runner.add_session("sess-1", lines)

    # Pretend we already emitted the first 3 lines
    runner.set_offset("sess-1", 3)
    runner.output_file.parent.mkdir(parents=True, exist_ok=True)
    _write_lines(runner.output_file, lines[:3])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    output_uuids = runner.get_output_uuids()
    assert output_uuids == uuids
    assert runner.get_offset("sess-1") == 5


def test_reconciliation_after_crash(tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str) -> None:
    """If we crashed after emitting but before saving the offset, reconciliation
    should find the true offset by scanning backwards."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    uuids = [uuid4().hex for _ in range(5)]
    lines = [_make_jsonl_line(u) for u in uuids]
    runner.add_session("sess-1", lines)

    # Simulate crash: offset says 2 (stale), but output has lines 1-4
    runner.set_offset("sess-1", 2)
    runner.output_file.parent.mkdir(parents=True, exist_ok=True)
    _write_lines(runner.output_file, lines[:4])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    output_uuids = runner.get_output_uuids()
    # Reconciliation should find offset=4, then emit only line 5
    assert output_uuids == uuids
    assert runner.get_offset("sess-1") == 5


def test_missing_session_file_no_crash(tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str) -> None:
    """If a session ID is in the history but its file doesn't exist,
    the script should not crash and should produce partial output."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)

    uuids_real = [uuid4().hex for _ in range(2)]
    runner.add_session("sess-real", [_make_jsonl_line(u) for u in uuids_real])

    # Add a session ID to history without creating its file
    with runner.history_file.open("a") as f:
        f.write("sess-missing hook\n")

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    assert runner.get_output_uuids() == uuids_real


def test_waiting_for_an_unwritten_session_file_spawns_no_find(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    """A never-messaged chat's session has no file, and its poller looks for one every
    cycle for as long as the chat stays idle; that lookup must not spawn a process.
    The file must still be picked up once Claude writes it."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    with runner.history_file.open("a") as f:
        f.write("sess-unwritten hook\n")

    real_find = shutil.which("find")
    assert real_find is not None
    find_calls_log = tmp_path / "find_calls.log"
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    find_shim = shim_dir / "find"
    find_shim.write_text(
        f'#!/usr/bin/env bash\necho "$*" >> {shlex.quote(str(find_calls_log))}\nexec {shlex.quote(real_find)} "$@"\n'
    )
    find_shim.chmod(0o755)

    result = runner.run_single_pass(path_prefix=shim_dir)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert not find_calls_log.exists(), f"find was spawned: {find_calls_log.read_text()}"
    assert runner.get_output_lines() == []

    uuids = [uuid4().hex for _ in range(2)]
    _write_lines(
        runner.claude_projects_dir / "encoded-work-dir" / "sess-unwritten.jsonl", [_make_jsonl_line(u) for u in uuids]
    )
    result = runner.run_single_pass(path_prefix=shim_dir)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_uuids() == uuids


def test_empty_session_file(tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str) -> None:
    """An empty session file should be handled gracefully."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    runner.add_session("sess-empty", [])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_lines() == []
    assert runner.get_offset("sess-empty") == 0


def test_no_duplicate_emission(tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str) -> None:
    """Running the script twice should not produce duplicates."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    uuids = [uuid4().hex for _ in range(3)]
    lines = [_make_jsonl_line(u) for u in uuids]
    runner.add_session("sess-1", lines)

    # First pass
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_uuids() == uuids

    # Second pass (no new lines)
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_uuids() == uuids


def test_incremental_emission(tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str) -> None:
    """Adding lines to a session file between passes should emit only new lines."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    uuids_batch1 = [uuid4().hex for _ in range(2)]
    session_file = runner.add_session("sess-1", [_make_jsonl_line(u) for u in uuids_batch1])

    # First pass
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_uuids() == uuids_batch1
    assert runner.get_offset("sess-1") == 2

    # Append more lines to the session file
    uuids_batch2 = [uuid4().hex for _ in range(3)]
    with session_file.open("a") as f:
        for u in uuids_batch2:
            f.write(_make_jsonl_line(u) + "\n")

    # Second pass
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_uuids() == uuids_batch1 + uuids_batch2
    assert runner.get_offset("sess-1") == 5


def test_multiple_sessions_concurrent_writes(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    """Both sessions can have new lines appended and both should be emitted."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    uuids_a = [uuid4().hex for _ in range(2)]
    uuids_b = [uuid4().hex for _ in range(2)]
    file_a = runner.add_session("sess-a", [_make_jsonl_line(u) for u in uuids_a])
    file_b = runner.add_session("sess-b", [_make_jsonl_line(u) for u in uuids_b])

    # First pass
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    first_uuids = set(runner.get_output_uuids())
    assert first_uuids == set(uuids_a + uuids_b)

    # Append to both sessions
    new_a = uuid4().hex
    new_b = uuid4().hex
    with file_a.open("a") as f:
        f.write(_make_jsonl_line(new_a) + "\n")
    with file_b.open("a") as f:
        f.write(_make_jsonl_line(new_b) + "\n")

    # Second pass
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    all_uuids = set(runner.get_output_uuids())
    assert all_uuids == set(uuids_a + uuids_b + [new_a, new_b])


def test_reconciliation_with_empty_output(tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str) -> None:
    """If the output file is empty but offset is stored, reconciliation
    should reset to 0 and re-emit everything."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    uuids = [uuid4().hex for _ in range(3)]
    lines = [_make_jsonl_line(u) for u in uuids]
    runner.add_session("sess-1", lines)

    # Stale offset with no output
    runner.set_offset("sess-1", 3)

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_uuids() == uuids
    assert runner.get_offset("sess-1") == 3


def test_offset_clamped_to_file_size(tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str) -> None:
    """If the stored offset exceeds the file size, reconciliation should
    handle it gracefully."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    uuids = [uuid4().hex for _ in range(2)]
    lines = [_make_jsonl_line(u) for u in uuids]
    runner.add_session("sess-1", lines)

    runner.set_offset("sess-1", 100)

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_uuids() == uuids
    assert runner.get_offset("sess-1") == 2


def test_offset_directory_created(tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str) -> None:
    """The offset directory should be created if it doesn't exist."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    runner.add_session("sess-1", [_make_jsonl_line()])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.offset_dir.exists()
    assert runner.get_offset("sess-1") == 1


def test_offset_stored_in_plugin_claude_directory(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    """Offsets should be stored under plugin/claude/.transcript_offsets/."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    runner.add_session("sess-1", [_make_jsonl_line()])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    expected = runner.agent_state_dir / "plugin" / "claude" / ".transcript_offsets" / "sess-1"
    assert expected.exists()
    assert expected.read_text().strip() == "1"


def test_session_added_after_initial_load(tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str) -> None:
    """A session added to the history file after the first pass should be
    picked up on the second pass."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    uuids_a = [uuid4().hex for _ in range(2)]
    runner.add_session("sess-a", [_make_jsonl_line(u) for u in uuids_a])

    # First pass
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_uuids() == uuids_a

    # Add new session
    uuids_b = [uuid4().hex for _ in range(2)]
    runner.add_session("sess-b", [_make_jsonl_line(u) for u in uuids_b])

    # Second pass
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    all_uuids = runner.get_output_uuids()
    assert set(all_uuids) == set(uuids_a + uuids_b)


def test_history_with_extra_fields(tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str) -> None:
    """History lines with extra fields should correctly extract just the session ID."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    uuid1 = uuid4().hex
    session_file = runner.claude_projects_dir / "proj1" / "sess-src.jsonl"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    _write_lines(session_file, [_make_jsonl_line(uuid1)])

    runner.history_file.parent.mkdir(parents=True, exist_ok=True)
    runner.history_file.write_text("sess-src hook_type extra_field\n")

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_uuids() == [uuid1]


def test_duplicate_session_id_in_history(tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str) -> None:
    """Duplicate session IDs in the history file should not cause duplicates."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    uuids = [uuid4().hex for _ in range(2)]
    runner.add_session("sess-dup", [_make_jsonl_line(u) for u in uuids])

    # Add same session ID again
    with runner.history_file.open("a") as f:
        f.write("sess-dup hook\n")

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_uuids() == uuids


# The empty-file (0-line) boundary is covered by test_empty_session_file, so this
# only exercises a single representative non-zero size; 10/50 walked the identical
# code path and were redundant.
@pytest.mark.parametrize("line_count", [10])
def test_various_file_sizes(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str, line_count: int
) -> None:
    """The script should handle a multi-line session file."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    uuids = [uuid4().hex for _ in range(line_count)]
    lines = [_make_jsonl_line(u) for u in uuids]
    runner.add_session("sess-sized", lines)

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_uuids() == uuids
    assert runner.get_offset("sess-sized") == line_count


def test_reconciliation_finds_highest_emitted_line(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    """Reconciliation should find the LAST emitted line, not just any emitted line."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    uuids = [uuid4().hex for _ in range(10)]
    lines = [_make_jsonl_line(u) for u in uuids]
    runner.add_session("sess-1", lines)

    # Simulate: offset says 3, but output has lines 1-8
    runner.set_offset("sess-1", 3)
    runner.output_file.parent.mkdir(parents=True, exist_ok=True)
    _write_lines(runner.output_file, lines[:8])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    # Reconciliation should find offset=8, emit lines 9-10
    output_uuids = runner.get_output_uuids()
    assert output_uuids == uuids
    assert runner.get_offset("sess-1") == 10


def test_multiple_sessions_partial_reconciliation(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    """When one session needs reconciliation and another doesn't, both
    should end up with correct offsets."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    uuids_a = [uuid4().hex for _ in range(4)]
    uuids_b = [uuid4().hex for _ in range(4)]
    lines_a = [_make_jsonl_line(u) for u in uuids_a]
    lines_b = [_make_jsonl_line(u) for u in uuids_b]

    runner.add_session("sess-a", lines_a)
    runner.add_session("sess-b", lines_b)

    # sess-a: stale offset 1, output has lines 1-3
    # sess-b: correct offset 4, output has lines 1-4
    runner.set_offset("sess-a", 1)
    runner.set_offset("sess-b", 4)
    runner.output_file.parent.mkdir(parents=True, exist_ok=True)
    _write_lines(runner.output_file, lines_a[:3] + lines_b[:4])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    output_uuids = set(runner.get_output_uuids())
    assert output_uuids == set(uuids_a + uuids_b)
    assert runner.get_offset("sess-a") == 4
    assert runner.get_offset("sess-b") == 4


# Claude fires no hook when a turn is interrupted in the terminal (Esc), so the
# `active` marker created at prompt submit is stranded. The streamer heals it
# when the current session's last record is an interrupt sentinel that postdates
# the marker. These tests require jq (like the hooks the heal mirrors).

_REQUIRES_JQ = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq is required by the interrupt-stranded marker heal"
)


def _assert_single_pass_leaves_marker(runner: ScriptRunner, marker: Path) -> None:
    """Run one pass and check the heal was skipped: the marker stays and no activity event is emitted."""
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert marker.exists()
    assert runner.get_activity_event_lines() == []


@_REQUIRES_JQ
@pytest.mark.parametrize(
    "sentinel_text", ["[Request interrupted by user]", "[Request interrupted by user for tool use]"]
)
@pytest.mark.parametrize("is_string_content", [False, True])
def test_single_pass_heals_marker_stranded_by_trailing_interrupt_sentinel(
    tmp_path: Path,
    stub_mngr_log_sh: str,
    mngr_transcript_lib_sh: str,
    sentinel_text: str,
    is_string_content: bool,
) -> None:
    """Both sentinel shapes, in both content forms, clear the stranded markers and emit one activity event."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    now = time.time()
    content: Any = sentinel_text if is_string_content else [{"type": "text", "text": sentinel_text}]
    runner.add_session("sess-cur", [_make_jsonl_line(), _make_user_record_line(content, now)])
    runner.set_current_session("sess-cur")
    marker = runner.create_active_marker(now - 300.0)
    permissions_marker = runner.agent_state_dir / "permissions_waiting"
    permissions_marker.touch()

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    assert not marker.exists()
    assert not permissions_marker.exists()
    # Like every other idle path, so idle-time consumers (auto-compaction) see the agent idle.
    assert (runner.agent_state_dir / "idle_since").read_text().strip().endswith("Z")
    event_lines = runner.get_activity_event_lines()
    assert len(event_lines) == 1
    event = json.loads(event_lines[0])
    assert event["type"] == "activity"
    assert event["reason"] == "interrupt_heal"
    # The sentinel line itself still streams into the output like any other line.
    assert len(runner.get_output_lines()) == 2


@_REQUIRES_JQ
def test_single_pass_keeps_marker_touched_after_the_interrupt(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    """A marker at/after the sentinel belongs to a newer turn (an immediate resubmit): keep it."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    now = time.time()
    sentinel = _make_interrupt_sentinel_line(now - 30.0)
    runner.add_session("sess-cur", [sentinel])
    runner.set_current_session("sess-cur")
    marker = runner.create_active_marker(now)

    _assert_single_pass_leaves_marker(runner, marker)


@_REQUIRES_JQ
def test_single_pass_ignores_tool_result_quoting_the_sentinel(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    """A trailing tool_result that merely quotes the sentinel (e.g. an agent grepping its own session) never heals."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    now = time.time()
    quoting_tool_result = _make_user_record_line(
        [{"type": "tool_result", "tool_use_id": "tool-1", "content": "[Request interrupted by user]"}], now
    )
    runner.add_session("sess-cur", [quoting_tool_result])
    runner.set_current_session("sess-cur")
    marker = runner.create_active_marker(now - 300.0)

    _assert_single_pass_leaves_marker(runner, marker)


@_REQUIRES_JQ
def test_single_pass_heal_without_marker_emits_nothing(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    """A trailing sentinel with no marker (the turn already settled) heals nothing and emits no event."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    sentinel = _make_interrupt_sentinel_line(time.time())
    runner.add_session("sess-cur", [sentinel])
    runner.set_current_session("sess-cur")

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    assert not (runner.agent_state_dir / "active").exists()
    assert runner.get_activity_event_lines() == []


@_REQUIRES_JQ
def test_single_pass_ignores_sentinel_in_non_current_session(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    """Only the CURRENT session's tail heals: an old session's interrupt is history."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    now = time.time()
    old_sentinel = _make_interrupt_sentinel_line(now)
    runner.add_session("sess-old", [old_sentinel])
    runner.add_session("sess-new", [_make_jsonl_line()])
    runner.set_current_session("sess-new")
    marker = runner.create_active_marker(now - 300.0)

    _assert_single_pass_leaves_marker(runner, marker)


@_REQUIRES_JQ
def test_single_pass_survives_an_empty_session_id_marker(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    """An empty claude_session_id names no session to heal from, and must not take the streamer down."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    now = time.time()
    sentinel = _make_interrupt_sentinel_line(now)
    runner.add_session("sess-cur", [sentinel])
    runner.set_current_session("")
    marker = runner.create_active_marker(now - 300.0)

    _assert_single_pass_leaves_marker(runner, marker)


@_REQUIRES_JQ
def test_single_pass_ignores_sentinel_without_timestamp(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    """A sentinel record with no parseable timestamp is skipped: the heal acts only on positive evidence."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    sentinel = _make_interrupt_sentinel_line(None)
    runner.add_session("sess-cur", [sentinel])
    runner.set_current_session("sess-cur")
    marker = runner.create_active_marker(time.time() - 300.0)

    _assert_single_pass_leaves_marker(runner, marker)


@_REQUIRES_JQ
@pytest.mark.parametrize("is_prompt_ahead_of_sentinel", [False, True])
def test_single_pass_keeps_marker_while_an_interrupt_flushes_a_queued_prompt(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str, is_prompt_ahead_of_sentinel: bool
) -> None:
    """An interrupt with a prompt queued dequeues it around the sentinel and starts its turn with no hook.

    The marker dates from the enqueue (UserPromptSubmit fires then), so only the dequeue after the
    interrupted turn's last reply tells this from a plain interrupt. Claude writes the flushed prompt on
    either side of the sentinel; both orders were seen live.
    """
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    now = time.time()
    flushed_prompt = _make_user_record_line("Queued follow-up: run sleep 21", now - 0.03)
    tool_result = _make_user_record_line(
        [{"type": "tool_result", "tool_use_id": "tool-1", "content": ""}], now - 0.004
    )
    sentinel = _make_interrupt_sentinel_line(now, "[Request interrupted by user for tool use]")
    interrupted_turn = [
        _make_user_record_line("Run sleep 44 in the foreground", now - 10.5),
        _make_assistant_record_line(now - 10.0),
        _make_queue_operation_line("enqueue", now - 5.6),
        _make_queue_operation_line("dequeue", now - 0.07),
    ]
    tail = [flushed_prompt, tool_result, sentinel] if is_prompt_ahead_of_sentinel else [tool_result, sentinel]
    runner.add_session("sess-cur", interrupted_turn + tail)
    runner.set_current_session("sess-cur")
    marker = runner.create_active_marker(now - 5.5)

    _assert_single_pass_leaves_marker(runner, marker)


@_REQUIRES_JQ
def test_single_pass_heals_an_interrupted_turn_that_a_dequeued_prompt_started(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    """A turn started from the queue (a background task's notification) and then interrupted still heals."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    now = time.time()
    runner.add_session(
        "sess-cur",
        [
            _make_queue_operation_line("enqueue", now - 5.1),
            _make_queue_operation_line("dequeue", now - 5.07),
            _make_user_record_line("<task-notification>Background command completed</task-notification>", now - 5.05),
            _make_assistant_record_line(now - 2.9),
            _make_user_record_line([{"type": "tool_result", "tool_use_id": "tool-1", "content": "rejected"}], now),
            _make_interrupt_sentinel_line(now, "[Request interrupted by user for tool use]"),
        ],
    )
    runner.set_current_session("sess-cur")
    marker = runner.create_active_marker(now - 4.9)

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    assert not marker.exists()
    assert len(runner.get_activity_event_lines()) == 1
