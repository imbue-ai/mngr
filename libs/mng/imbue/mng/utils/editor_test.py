"""Unit tests for the editor module."""

from pathlib import Path

import pytest

from imbue.mng.errors import UserInputError
from imbue.mng.utils.editor import EditorSession
from imbue.mng.utils.editor import get_editor_command


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
    script_content = """#!/bin/bash
# Accept file argument but ignore it, just sleep
sleep 10
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
    # Use a long-running script so the process stays running
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that wait_for_result() returns content when editor exits successfully."""
    # Use 'true' which exits immediately with code 0
    monkeypatch.setenv("EDITOR", "true")
    with EditorSession.create() as session:
        # Write content to temp file before starting
        # (simulates what the user would do in the editor)
        session.temp_file_path.write_text("Edited content")
        session.start()
        result = session.wait_for_result()
        assert result == "Edited content"


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that wait_for_result() returns None when content is empty."""
    # Use 'true' which exits with code 0 but doesn't modify the file
    monkeypatch.setenv("EDITOR", "true")
    with EditorSession.create() as session:
        # File is empty by default after create
        session.start()
        result = session.wait_for_result()
        assert result is None


def test_editor_session_wait_for_result_strips_trailing_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that wait_for_result() strips trailing whitespace."""
    # Use 'true' which exits with code 0 but doesn't modify the file
    monkeypatch.setenv("EDITOR", "true")
    with EditorSession.create() as session:
        # Write content with trailing whitespace
        session.temp_file_path.write_text("Content with whitespace  \n\n")
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


def test_editor_session_cleanup_handles_stubborn_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that cleanup() can handle a process that requires killing."""
    # Create a script that ignores SIGTERM
    script_content = """#!/bin/bash
trap "" SIGTERM
sleep 100
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
