"""Tests for stream_transcript.sh.

Exercises the script's core behaviors by running it with --single-pass in a
controlled filesystem layout. Each test sets up:
  - A fake agent state dir with session history, session JSONL files, and a stub mng_log.sh
  - A fake ~/.claude/projects/ directory (via HOME override)

The --single-pass flag makes the script run initialization + one poll cycle
then exit, so tests are fast and deterministic.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.mng.utils.testing import get_short_random_string

# -- Helpers --


def _make_jsonl_line(uuid: str | None = None) -> str:
    """Create a minimal Claude session JSONL line with a uuid field."""
    line_uuid = uuid if uuid is not None else uuid4().hex
    return json.dumps({"uuid": line_uuid, "type": "assistant", "timestamp": "2026-01-01T00:00:00Z"})


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


_STUB_LOG_SH = textwrap.dedent("""\
    #!/bin/bash
    log_info() { :; }
    log_debug() { :; }
    log_warn() { :; }
    log_error() { :; }
""")


class ScriptRunner:
    """Helper to run stream_transcript.sh in a test environment."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.agent_state_dir = tmp_path / "agent_state"
        # Fake HOME so ~/.claude/projects/ resolves to our test dir
        self.fake_home = tmp_path / "fakehome"
        self.claude_projects_dir = self.fake_home / ".claude" / "projects"

        # Create directory structure
        self.agent_state_dir.mkdir(parents=True)
        self.claude_projects_dir.mkdir(parents=True)
        (self.agent_state_dir / "commands").mkdir(parents=True)
        (self.agent_state_dir / "events" / "logs" / "stream_transcript").mkdir(parents=True)

        # Write stub mng_log.sh
        log_path = self.agent_state_dir / "commands" / "mng_log.sh"
        log_path.write_text(_STUB_LOG_SH)
        log_path.chmod(0o755)

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

    def run_single_pass(self, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
        """Run the script with --single-pass (initialize + one cycle + exit)."""
        env = {
            **os.environ,
            "MNG_AGENT_STATE_DIR": str(self.agent_state_dir),
            "HOME": str(self.fake_home),
        }
        result = subprocess.run(
            ["bash", str(self.script_path), "--single-pass"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return result


# -- Tests --


def test_empty_history_produces_no_output(tmp_path: Path) -> None:
    """With no history file, the script should produce no output."""
    runner = ScriptRunner(tmp_path)
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_lines() == []


def test_single_session_emits_all_lines(tmp_path: Path) -> None:
    """A single session with 3 lines should emit all 3 to the output."""
    runner = ScriptRunner(tmp_path)
    uuids = [uuid4().hex for _ in range(3)]
    lines = [_make_jsonl_line(u) for u in uuids]
    runner.add_session("sess-1", lines)

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    assert runner.get_output_uuids() == uuids
    assert runner.get_offset("sess-1") == 3


def test_multiple_sessions_emit_all(tmp_path: Path) -> None:
    """Multiple sessions should all have their lines emitted."""
    runner = ScriptRunner(tmp_path)
    uuids_a = [uuid4().hex for _ in range(2)]
    uuids_b = [uuid4().hex for _ in range(2)]

    runner.add_session("sess-a", [_make_jsonl_line(u) for u in uuids_a])
    runner.add_session("sess-b", [_make_jsonl_line(u) for u in uuids_b])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    output_uuids = runner.get_output_uuids()
    assert set(output_uuids) == set(uuids_a + uuids_b)
    assert len(output_uuids) == 4


def test_offset_tracking_skips_already_emitted(tmp_path: Path) -> None:
    """With a stored offset, only new lines should be emitted."""
    runner = ScriptRunner(tmp_path)
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


def test_reconciliation_after_crash(tmp_path: Path) -> None:
    """If we crashed after emitting but before saving the offset, reconciliation
    should find the true offset by scanning backwards."""
    runner = ScriptRunner(tmp_path)
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


def test_missing_session_file_no_crash(tmp_path: Path) -> None:
    """If a session ID is in the history but its file doesn't exist,
    the script should not crash and should produce partial output."""
    runner = ScriptRunner(tmp_path)

    uuids_real = [uuid4().hex for _ in range(2)]
    runner.add_session("sess-real", [_make_jsonl_line(u) for u in uuids_real])

    # Add a session ID to history without creating its file
    with runner.history_file.open("a") as f:
        f.write("sess-missing hook\n")

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    assert runner.get_output_uuids() == uuids_real


def test_empty_session_file(tmp_path: Path) -> None:
    """An empty session file should be handled gracefully."""
    runner = ScriptRunner(tmp_path)
    runner.add_session("sess-empty", [])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_lines() == []
    assert runner.get_offset("sess-empty") == 0


def test_no_duplicate_emission(tmp_path: Path) -> None:
    """Running the script twice should not produce duplicates."""
    runner = ScriptRunner(tmp_path)
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


def test_incremental_emission(tmp_path: Path) -> None:
    """Adding lines to a session file between passes should emit only new lines."""
    runner = ScriptRunner(tmp_path)
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


def test_multiple_sessions_concurrent_writes(tmp_path: Path) -> None:
    """Both sessions can have new lines appended and both should be emitted."""
    runner = ScriptRunner(tmp_path)
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


def test_reconciliation_with_empty_output(tmp_path: Path) -> None:
    """If the output file is empty but offset is stored, reconciliation
    should reset to 0 and re-emit everything."""
    runner = ScriptRunner(tmp_path)
    uuids = [uuid4().hex for _ in range(3)]
    lines = [_make_jsonl_line(u) for u in uuids]
    runner.add_session("sess-1", lines)

    # Stale offset with no output
    runner.set_offset("sess-1", 3)

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_uuids() == uuids
    assert runner.get_offset("sess-1") == 3


def test_offset_clamped_to_file_size(tmp_path: Path) -> None:
    """If the stored offset exceeds the file size, reconciliation should
    handle it gracefully."""
    runner = ScriptRunner(tmp_path)
    uuids = [uuid4().hex for _ in range(2)]
    lines = [_make_jsonl_line(u) for u in uuids]
    runner.add_session("sess-1", lines)

    runner.set_offset("sess-1", 100)

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_uuids() == uuids
    assert runner.get_offset("sess-1") == 2


def test_offset_directory_created(tmp_path: Path) -> None:
    """The offset directory should be created if it doesn't exist."""
    runner = ScriptRunner(tmp_path)
    runner.add_session("sess-1", [_make_jsonl_line()])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.offset_dir.exists()
    assert runner.get_offset("sess-1") == 1


def test_offset_stored_in_plugin_claude_directory(tmp_path: Path) -> None:
    """Offsets should be stored under plugin/claude/.transcript_offsets/."""
    runner = ScriptRunner(tmp_path)
    runner.add_session("sess-1", [_make_jsonl_line()])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    expected = runner.agent_state_dir / "plugin" / "claude" / ".transcript_offsets" / "sess-1"
    assert expected.exists()
    assert expected.read_text().strip() == "1"


def test_session_added_after_initial_load(tmp_path: Path) -> None:
    """A session added to the history file after the first pass should be
    picked up on the second pass."""
    runner = ScriptRunner(tmp_path)
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


def test_history_with_extra_fields(tmp_path: Path) -> None:
    """History lines with extra fields should correctly extract just the session ID."""
    runner = ScriptRunner(tmp_path)
    uuid1 = uuid4().hex
    session_file = runner.claude_projects_dir / "proj1" / "sess-src.jsonl"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    _write_lines(session_file, [_make_jsonl_line(uuid1)])

    runner.history_file.parent.mkdir(parents=True, exist_ok=True)
    runner.history_file.write_text("sess-src hook_type extra_field\n")

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_uuids() == [uuid1]


def test_duplicate_session_id_in_history(tmp_path: Path) -> None:
    """Duplicate session IDs in the history file should not cause duplicates."""
    runner = ScriptRunner(tmp_path)
    uuids = [uuid4().hex for _ in range(2)]
    runner.add_session("sess-dup", [_make_jsonl_line(u) for u in uuids])

    # Add same session ID again
    with runner.history_file.open("a") as f:
        f.write("sess-dup hook\n")

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_uuids() == uuids


@pytest.mark.parametrize("line_count", [1, 10, 50])
def test_various_file_sizes(tmp_path: Path, line_count: int) -> None:
    """The script should handle session files of various sizes."""
    runner = ScriptRunner(tmp_path)
    uuids = [uuid4().hex for _ in range(line_count)]
    lines = [_make_jsonl_line(u) for u in uuids]
    runner.add_session("sess-sized", lines)

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_uuids() == uuids
    assert runner.get_offset("sess-sized") == line_count


def test_reconciliation_finds_highest_emitted_line(tmp_path: Path) -> None:
    """Reconciliation should find the LAST emitted line, not just any emitted line."""
    runner = ScriptRunner(tmp_path)
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


def test_multiple_sessions_partial_reconciliation(tmp_path: Path) -> None:
    """When one session needs reconciliation and another doesn't, both
    should end up with correct offsets."""
    runner = ScriptRunner(tmp_path)
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
