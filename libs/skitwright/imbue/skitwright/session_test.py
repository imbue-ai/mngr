import os
from pathlib import Path
from uuid import uuid4

from imbue.skitwright.data_types import OutputLine
from imbue.skitwright.data_types import OutputSource
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


def test_session_run_captures_stderr(tmp_path: Path) -> None:
    session = Session(cwd=tmp_path)
    result = session.run("echo err >&2")
    assert "err" in result.stderr


def test_session_run_separates_and_records_both_stdout_and_stderr(tmp_path: Path) -> None:
    session = Session(cwd=tmp_path)

    result = session.run("printf 'out1\\nout2\\n'; printf 'err1\\n' >&2")

    # The runner must keep the two streams separate when reconstructing them.
    assert result.stdout == "out1\nout2\n"
    assert result.stderr == "err1\n"
    # Every produced line must be recorded in output_lines tagged with its real source.
    # The cross-stream ordering depends on the OS scheduler, so assert on contents, not order.
    assert OutputLine(source=OutputSource.STDOUT, text="out1") in result.output_lines
    assert OutputLine(source=OutputSource.STDOUT, text="out2") in result.output_lines
    assert OutputLine(source=OutputSource.STDERR, text="err1") in result.output_lines
    assert len(result.output_lines) == 3


def test_session_run_honors_cwd(tmp_path: Path) -> None:
    session = Session(cwd=tmp_path)

    result = session.run("pwd")

    # resolve() tolerates the macOS /private symlink in front of temp dirs.
    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_session_run_honors_env(tmp_path: Path) -> None:
    env_var_name = f"SKITWRIGHT_TEST_VAR_{uuid4().hex}"
    env_var_value = uuid4().hex
    session = Session(env={**os.environ, env_var_name: env_var_value}, cwd=tmp_path)

    result = session.run(f"echo ${env_var_name}")

    assert result.stdout.strip() == env_var_value


def test_session_run_records_comment_in_transcript(tmp_path: Path) -> None:
    session = Session(cwd=tmp_path)
    comment = f"setup step {uuid4().hex}"

    session.run("echo hi", comment=comment)

    assert f"# {comment}" in session.transcript


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
    result = session.run("sleep 38217", timeout=0.1)
    assert result.exit_code == 124
    assert "timed out" in result.stderr
