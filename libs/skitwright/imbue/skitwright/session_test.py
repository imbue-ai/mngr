from pathlib import Path

from imbue.skitwright.session import Session


def test_session_run_captures_stdout(tmp_path: Path) -> None:
    session = Session(cwd=tmp_path)
    result = session.run("echo hello")
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"


def test_session_run_captures_exit_code(tmp_path: Path) -> None:
    session = Session(cwd=tmp_path)
    result = session.run("exit 42")
    assert result.exit_code == 42
    assert result.exit_code != 0


def test_session_run_captures_stderr(tmp_path: Path) -> None:
    session = Session(cwd=tmp_path)
    result = session.run("echo err >&2")
    assert "err" in result.stderr


def test_session_transcript_records_all_commands(tmp_path: Path) -> None:
    session = Session(cwd=tmp_path)
    session.run("echo first")
    session.run("echo second")
    transcript = session.transcript
    assert "$ echo first" in transcript
    assert "$ echo second" in transcript
    assert "first" in transcript
    assert "second" in transcript


def test_session_run_timeout(tmp_path: Path) -> None:
    session = Session(cwd=tmp_path)
    result = session.run("sleep 60", timeout=0.1)
    assert result.exit_code == 124
    assert "timed out" in result.stderr
