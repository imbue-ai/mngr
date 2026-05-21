"""Tests for the gemini stream_transcript.sh raw streamer.

Exercises the script's core behaviors by running it with --single-pass in a
controlled filesystem layout. Each test sets up:
  - A fake agent state dir (where the raw output ends up)
  - A fake gemini config dir with one or more session dirs, each containing
    a `.project_root` file pointing at the agent's work dir and a `chats/`
    subdirectory with session JSONL files
  - A stub mngr_log.sh (no-op logging)

The streamer's contract: copy lines verbatim from each matching session
file into ``$MNGR_AGENT_STATE_DIR/logs/gemini_transcript/events.jsonl``,
tracking per-session offsets so it picks up new lines on the next pass
without re-emitting anything.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


class ScriptRunner:
    """Helper to run gemini stream_transcript.sh in a test environment."""

    def __init__(self, tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str) -> None:
        self.tmp_path = tmp_path
        self.agent_state_dir = tmp_path / "agent_state"
        self.gemini_dir = tmp_path / "gemini"
        self.work_dir = tmp_path / "work"

        self.agent_state_dir.mkdir(parents=True)
        (self.agent_state_dir / "commands").mkdir(parents=True)
        self.gemini_dir.mkdir(parents=True)
        (self.gemini_dir / "tmp").mkdir(parents=True)
        self.work_dir.mkdir(parents=True)

        log_path = self.agent_state_dir / "commands" / "mngr_log.sh"
        log_path.write_text(stub_mngr_log_sh)
        log_path.chmod(0o755)

        lib_path = self.agent_state_dir / "commands" / "mngr_transcript_lib.sh"
        lib_path.write_text(mngr_transcript_lib_sh)
        lib_path.chmod(0o755)

        self.script_path = Path(__file__).parent / "stream_transcript.sh"
        self.output_file = self.agent_state_dir / "logs" / "gemini_transcript" / "events.jsonl"
        self._session_counter = 0

    def add_session(self, lines: list[str], project_root: Path | None = None) -> Path:
        """Create a new gemini session dir with one session file and return its path."""
        self._session_counter += 1
        session_dir = self.gemini_dir / "tmp" / f"sess-{self._session_counter}"
        (session_dir / "chats").mkdir(parents=True)
        (session_dir / ".project_root").write_text(str(project_root or self.work_dir))
        session_file = session_dir / "chats" / f"session-{self._session_counter}.jsonl"
        session_file.write_text(("\n".join(lines) + "\n") if lines else "")
        return session_file

    def append_to_session(self, session_file: Path, lines: list[str]) -> None:
        with session_file.open("a") as f:
            for line in lines:
                f.write(line + "\n")

    def get_output_lines(self) -> list[str]:
        if not self.output_file.exists():
            return []
        return [line for line in self.output_file.read_text().splitlines() if line.strip()]

    def run_single_pass(self, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "MNGR_AGENT_STATE_DIR": str(self.agent_state_dir),
            "MNGR_AGENT_WORK_DIR": str(self.work_dir),
            "GEMINI_CONFIG_DIR": str(self.gemini_dir),
        }
        return subprocess.run(
            ["bash", str(self.script_path), "--single-pass"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )


def _user_line(uuid: str, text: str) -> str:
    return json.dumps({"id": uuid, "type": "user", "timestamp": "2026-01-01T00:00:00Z", "content": [{"text": text}]})


def test_empty_tmp_dir_produces_no_output(tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_lines() == []


def test_session_with_matching_project_root_is_emitted(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    runner.add_session([_user_line("uuid-1", "hello"), _user_line("uuid-2", "world")])

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"

    lines = runner.get_output_lines()
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == "uuid-1"
    assert json.loads(lines[1])["id"] == "uuid-2"


def test_session_with_mismatched_project_root_is_skipped(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    other_work_dir = tmp_path / "other_work"
    other_work_dir.mkdir()
    runner.add_session([_user_line("uuid-skip", "ignored")], project_root=other_work_dir)

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_lines() == []


def test_session_dir_without_project_root_is_skipped(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    bad_dir = runner.gemini_dir / "tmp" / "no-marker"
    (bad_dir / "chats").mkdir(parents=True)
    (bad_dir / "chats" / "session-1.jsonl").write_text(_user_line("uuid-x", "x") + "\n")

    result = runner.run_single_pass()
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert runner.get_output_lines() == []


def test_incremental_emission_via_offset(tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str) -> None:
    """A second pass should pick up only the newly appended lines, not re-emit old ones."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    session_file = runner.add_session([_user_line("uuid-1", "first")])

    runner.run_single_pass()
    assert len(runner.get_output_lines()) == 1

    runner.append_to_session(session_file, [_user_line("uuid-2", "second")])
    runner.run_single_pass()

    lines = runner.get_output_lines()
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == "uuid-1"
    assert json.loads(lines[1])["id"] == "uuid-2"


def test_late_appearing_session_file_is_picked_up(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    """A session that appears after the first pass should still be streamed on the next pass."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    runner.run_single_pass()
    assert runner.get_output_lines() == []

    runner.add_session([_user_line("uuid-late", "late")])
    runner.run_single_pass()

    lines = runner.get_output_lines()
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == "uuid-late"


def test_multiple_session_files_in_same_session_dir(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    """A session dir with multiple session-*.jsonl files emits all of them."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    session_dir = runner.gemini_dir / "tmp" / "sess-multi"
    (session_dir / "chats").mkdir(parents=True)
    (session_dir / ".project_root").write_text(str(runner.work_dir))
    (session_dir / "chats" / "session-A.jsonl").write_text(_user_line("uuid-A", "from A") + "\n")
    (session_dir / "chats" / "session-B.jsonl").write_text(_user_line("uuid-B", "from B") + "\n")

    runner.run_single_pass()

    ids = {json.loads(line)["id"] for line in runner.get_output_lines()}
    assert ids == {"uuid-A", "uuid-B"}


def test_offset_reconciliation_recovers_after_lost_offset(
    tmp_path: Path, stub_mngr_log_sh: str, mngr_transcript_lib_sh: str
) -> None:
    """If the offset file is removed (simulating crash before save), reconciliation should avoid re-emitting."""
    runner = ScriptRunner(tmp_path, stub_mngr_log_sh, mngr_transcript_lib_sh)
    runner.add_session([_user_line("uuid-1", "first"), _user_line("uuid-2", "second")])

    runner.run_single_pass()
    assert len(runner.get_output_lines()) == 2

    # Wipe the offset dir, then run again -- reconciliation should detect the existing
    # output via id lookup and not re-append the already-emitted lines.
    offset_dir = runner.agent_state_dir / "plugin" / "gemini" / ".transcript_offsets"
    for child in offset_dir.iterdir():
        child.unlink()

    runner.run_single_pass()
    assert len(runner.get_output_lines()) == 2
