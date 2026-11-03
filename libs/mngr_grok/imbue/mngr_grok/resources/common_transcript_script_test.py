import os
import signal
import subprocess
from pathlib import Path
from typing import Final

from imbue.mngr.utils.polling import poll_until
from imbue.mngr_grok.plugin import COMMON_TRANSCRIPT_SCRIPT_NAMES
from imbue.mngr_grok.resources.testing import SESSION_FIXTURES_DIR
from imbue.mngr_grok.resources.testing import cut_before_first_turn_completed
from imbue.mngr_grok.resources.testing import install_transcript_scripts
from imbue.mngr_grok.resources.testing import read_common_transcript_records
from imbue.mngr_grok.resources.testing import render_jsonl
from imbue.mngr_grok.resources.testing import run_agent_command_script
from imbue.mngr_grok.resources.testing import tag_native_lines

_SESSION_ID: Final[str] = "01a156d6-f927-7091-91a4-f6692a5800da"
_FIXTURE_SESSION: Final[Path] = SESSION_FIXTURES_DIR / "plain" / _SESSION_ID / "updates.jsonl"


def _stage_converter(tmp_path: Path, stub_mngr_log_sh: str) -> Path:
    state_dir = tmp_path / "agent-state"
    install_transcript_scripts(state_dir, COMMON_TRANSCRIPT_SCRIPT_NAMES, stub_mngr_log_sh)
    return state_dir


def _write_raw_transcript(state_dir: Path, native_lines: list[bytes]) -> None:
    raw_path = state_dir / "logs" / "grok_transcript" / "events.jsonl"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(render_jsonl(tag_native_lines(_SESSION_ID, None, native_lines)))


def _plain_lines_before_turn_end() -> list[bytes]:
    return cut_before_first_turn_completed(_FIXTURE_SESSION.read_bytes()).splitlines(keepends=True)


def test_single_pass_emits_steps_still_open_at_the_end_of_the_input(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_converter(tmp_path, stub_mngr_log_sh)
    _write_raw_transcript(state_dir, _plain_lines_before_turn_end())

    run_agent_command_script(state_dir, "common_transcript.sh", "--single-pass")

    assert [record["type"] for record in read_common_transcript_records(state_dir)] == ["header", "step", "step"]


def test_turn_end_single_pass_holds_steps_still_open(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_converter(tmp_path, stub_mngr_log_sh)
    _write_raw_transcript(state_dir, _plain_lines_before_turn_end())

    run_agent_command_script(state_dir, "common_transcript.sh", "--single-pass", "--hold-open-steps")

    assert [record.get("source") for record in read_common_transcript_records(state_dir)] == [None, "user"]


def test_single_pass_without_input_writes_nothing(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_converter(tmp_path, stub_mngr_log_sh)
    run_agent_command_script(state_dir, "common_transcript.sh", "--single-pass")

    assert read_common_transcript_records(state_dir) == []


def test_single_pass_skips_while_the_convert_lock_is_held(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_converter(tmp_path, stub_mngr_log_sh)
    _write_raw_transcript(state_dir, _FIXTURE_SESSION.read_bytes().splitlines(keepends=True))
    lock_dir = state_dir / ".common_transcript_convert.lock"
    lock_dir.mkdir()

    run_agent_command_script(
        state_dir, "common_transcript.sh", "--single-pass", extra_env={"MNGR_CONVERT_LOCK_TIMEOUT": "0"}
    )
    assert read_common_transcript_records(state_dir) == []

    lock_dir.rmdir()
    run_agent_command_script(state_dir, "common_transcript.sh", "--single-pass")
    assert [record["type"] for record in read_common_transcript_records(state_dir)] == ["header", "step", "step"]


def test_daemon_converts_the_input_and_holds_the_open_step(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_converter(tmp_path, stub_mngr_log_sh)
    _write_raw_transcript(state_dir, _plain_lines_before_turn_end())
    watcher = subprocess.Popen(
        ["bash", str(state_dir / "commands" / "common_transcript.sh")],
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert poll_until(lambda: len(read_common_transcript_records(state_dir)) == 2, timeout=5.0)
        assert watcher.poll() is None
    finally:
        os.killpg(watcher.pid, signal.SIGTERM)
        watcher.wait(timeout=10)

    assert [record.get("source") for record in read_common_transcript_records(state_dir)] == [None, "user"]
