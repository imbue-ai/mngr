"""Unit tests for the editor module."""

import threading
from pathlib import Path

import pytest

from imbue.mngr.errors import UserInputError
from imbue.mngr.utils.editor import EditorSession
from imbue.mngr.utils.editor import get_editor_command
from imbue.mngr.utils.polling import poll_until


def _create_executable_script(tmp_path: Path, name: str, content: str) -> Path:
    """Create an executable script in the given directory."""
    script_path = tmp_path / name
    script_path.write_text(content)
    script_path.chmod(0o755)
    return script_path


@pytest.fixture
def long_running_editor(tmp_path: Path) -> Path:
    """Create a temporary script that acts as a long-running editor.

    The script ignores its file argument and just sleeps, which is useful
    for testing process management without the editor exiting immediately.
    """
    # Use a large, globally-unique sleep duration so the session-cleanup leak
    # detector (which scans for leftover child processes by name/args) can't
    # misattribute an unrelated test's stray `sleep` to this one. The process
    # is terminated on cleanup regardless of the duration, so a long sleep is
    # harmless and just guarantees the editor outlives every synchronous
    # is_running() assertion below.
    script_content = """#!/bin/bash
# Accept file argument but ignore it, just sleep
sleep 38291
"""
    return _create_executable_script(tmp_path, "long_editor.sh", script_content)


def test_get_editor_command_uses_visual_env_var_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that $VISUAL is preferred over $EDITOR."""
    monkeypatch.setenv("VISUAL", "code")
    monkeypatch.setenv("EDITOR", "vim")
    assert get_editor_command() == "code"


def test_get_editor_command_uses_editor_when_visual_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that $EDITOR is used when $VISUAL is not set."""
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "nano")
    assert get_editor_command() == "nano"


def test_get_editor_command_falls_back_to_default_when_no_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that a fallback editor is used when env vars are not set."""
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    result = get_editor_command()
    # Should find one of the fallback editors or return vim as last resort
    assert result in ("vim", "vi", "nano", "notepad")


def test_editor_session_create_with_no_initial_content() -> None:
    """Test creating a session with no initial content."""
    with EditorSession.create() as session:
        assert session.temp_file_path.exists()
        assert session.temp_file_path.read_text() == ""


def test_editor_session_create_with_initial_content() -> None:
    """Test creating a session with initial content."""
    with EditorSession.create(initial_content="Hello World") as session:
        assert session.temp_file_path.exists()
        assert session.temp_file_path.read_text() == "Hello World"


def test_editor_session_start_raises_if_already_started(
    long_running_editor: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that start() raises if session was already started."""
    # Use a long-running script so the process doesn't exit immediately
    monkeypatch.setenv("EDITOR", str(long_running_editor))
    with EditorSession.create() as session:
        session.start()
        with pytest.raises(UserInputError, match="already started"):
            session.start()


def test_editor_session_is_running_returns_false_before_start() -> None:
    """Test that is_running() returns False before session is started."""
    with EditorSession.create() as session:
        assert session.is_running() is False


def test_editor_session_is_running_returns_true_when_process_running(
    long_running_editor: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that is_running() returns True when process is running."""
    # Use a long-running script so the process stays running. The long sleep in
    # long_running_editor guarantees the editor outlives this synchronous
    # assertion, so there is no race between start() and the is_running() check.
    monkeypatch.setenv("EDITOR", str(long_running_editor))
    with EditorSession.create() as session:
        session.start()
        assert session.is_running() is True


def test_editor_session_wait_for_result_raises_if_not_started() -> None:
    """Test that wait_for_result() raises if session not started."""
    with EditorSession.create() as session:
        with pytest.raises(UserInputError, match="not started"):
            session.wait_for_result()


def test_editor_session_wait_for_result_returns_content_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that wait_for_result() returns the content the editor wrote to its file argument."""
    # A fake editor that actually writes to its $1 file argument, proving the
    # result is read from what the editor produced (not pre-seeded into the file).
    editor = _create_executable_script(
        tmp_path,
        "writing_editor.sh",
        '#!/bin/bash\necho "Edited content" > "$1"\n',
    )
    monkeypatch.setenv("EDITOR", str(editor))
    with EditorSession.create() as session:
        session.start()
        result = session.wait_for_result()
        assert result == "Edited content"


@pytest.mark.allow_warnings(match=r"^Editor exited with non-zero code: 1")
def test_editor_session_wait_for_result_returns_none_on_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that wait_for_result() returns None when editor exits with error."""
    # Use 'false' which exits with code 1
    monkeypatch.setenv("EDITOR", "false")
    with EditorSession.create() as session:
        session.start()
        result = session.wait_for_result()
        assert result is None


def test_editor_session_wait_for_result_returns_none_on_empty_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that wait_for_result() returns None when the editor leaves only whitespace."""
    # A fake editor that writes only whitespace/newlines to its file argument;
    # after rstrip this is empty and should yield None.
    editor = _create_executable_script(
        tmp_path,
        "whitespace_only_editor.sh",
        '#!/bin/bash\nprintf "   \\n\\n" > "$1"\n',
    )
    monkeypatch.setenv("EDITOR", str(editor))
    with EditorSession.create(initial_content="seed") as session:
        session.start()
        result = session.wait_for_result()
        assert result is None


def test_editor_session_wait_for_result_strips_trailing_whitespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that wait_for_result() strips trailing whitespace the editor wrote."""
    # A fake editor that writes content with trailing whitespace/newlines to its
    # file argument; wait_for_result should return the rstripped content.
    editor = _create_executable_script(
        tmp_path,
        "trailing_whitespace_editor.sh",
        '#!/bin/bash\nprintf "Content with whitespace  \\n\\n" > "$1"\n',
    )
    monkeypatch.setenv("EDITOR", str(editor))
    with EditorSession.create() as session:
        session.start()
        result = session.wait_for_result()
        assert result == "Content with whitespace"


def test_editor_session_cleanup_removes_temp_file() -> None:
    """Test that cleanup() removes the temp file."""
    session = EditorSession.create()
    temp_path = session.temp_file_path
    assert temp_path.exists()

    session.cleanup()

    assert not temp_path.exists()


def test_editor_session_cleanup_terminates_running_process(
    long_running_editor: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that cleanup() terminates a running editor process."""
    # Use a long-running script so the process stays running
    monkeypatch.setenv("EDITOR", str(long_running_editor))
    with EditorSession.create() as session:
        session.start()
        # Verify process is running
        assert session.is_running() is True
        # Cleanup should terminate it
        session.cleanup()
        # Process should no longer be running
        assert session.is_running() is False


@pytest.mark.allow_warnings(match=r"^Editor process did not terminate gracefully")
def test_editor_session_cleanup_handles_stubborn_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that cleanup() can handle a process that requires killing."""
    # Create a script that ignores SIGTERM. Use a large, globally-unique sleep
    # duration (distinct from long_running_editor's) so the leak detector can't
    # confuse leftover processes between tests; cleanup() kills it regardless.
    script_content = """#!/bin/bash
trap "" SIGTERM
sleep 47213
"""
    script_path = _create_executable_script(tmp_path, "stubborn_editor.sh", script_content)

    monkeypatch.setenv("EDITOR", str(script_path))
    with EditorSession.create() as session:
        session.start()
        # Verify process is running
        assert session.is_running() is True
        # Cleanup should kill it after terminate fails
        session.cleanup()
        # Process should no longer be running (was killed)
        assert session.is_running() is False


def test_editor_session_is_finished_returns_false_before_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that is_finished() returns False before waiting for result."""
    monkeypatch.setenv("EDITOR", "true")
    with EditorSession.create() as session:
        session.start()
        # Process might have finished but we haven't called wait_for_result yet
        assert session.is_finished() is False


def test_editor_session_is_finished_returns_true_after_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that is_finished() returns True after waiting for result."""
    monkeypatch.setenv("EDITOR", "true")
    with EditorSession.create() as session:
        session.start()
        session.wait_for_result()
        assert session.is_finished() is True


def test_editor_session_on_exit_callback_runs_and_result_is_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that the on_exit callback fires once when the editor exits, and that
    wait_for_result() then returns the content cached by the monitor thread."""
    editor = _create_executable_script(
        tmp_path,
        "callback_editor.sh",
        '#!/bin/bash\necho "Async edited" > "$1"\nexit 0\n',
    )
    monkeypatch.setenv("EDITOR", str(editor))

    callback_done = threading.Event()
    call_count = 0

    def on_exit() -> None:
        nonlocal call_count
        call_count += 1
        callback_done.set()

    with EditorSession.create() as session:
        session.start(on_exit=on_exit)
        # The monitor thread should detect the editor exit and invoke the callback.
        assert poll_until(callback_done.is_set), "on_exit callback did not fire within timeout"
        # The callback ran, and the monitor thread cached the result, so
        # is_finished() should already be True before we call wait_for_result().
        assert poll_until(session.is_finished), "monitor thread did not mark session finished"
        # wait_for_result() should now hit the early-return cached-result branch.
        result = session.wait_for_result()
        assert result == "Async edited"
        # The callback must have run exactly once.
        assert call_count == 1


def test_editor_session_cleanup_after_finished_removes_temp_file_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that cleanup() after the editor finishes removes the temp file, and
    that a second cleanup() is a harmless no-op."""
    editor = _create_executable_script(
        tmp_path,
        "finishing_editor.sh",
        '#!/bin/bash\necho "Done editing" > "$1"\nexit 0\n',
    )
    monkeypatch.setenv("EDITOR", str(editor))

    session = EditorSession.create()
    temp_path = session.temp_file_path
    session.start()
    result = session.wait_for_result()
    assert result == "Done editing"
    assert temp_path.exists()

    session.cleanup()
    assert not temp_path.exists()

    # A second cleanup must not raise even though the temp file is already gone.
    session.cleanup()
    assert not temp_path.exists()
