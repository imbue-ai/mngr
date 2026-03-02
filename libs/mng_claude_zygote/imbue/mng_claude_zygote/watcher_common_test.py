"""Unit tests for watcher_common.py shared utilities."""

import threading
import time
from pathlib import Path
from typing import Any
from typing import cast

import pytest

from imbue.mng_claude_zygote.resources.watcher_common import ChangeHandler
from imbue.mng_claude_zygote.resources.watcher_common import Logger
from imbue.mng_claude_zygote.resources.watcher_common import mtime_poll_directories
from imbue.mng_claude_zygote.resources.watcher_common import mtime_poll_files
from imbue.mng_claude_zygote.resources.watcher_common import require_env
from imbue.mng_claude_zygote.resources.watcher_common import setup_watchdog_for_directories
from imbue.mng_claude_zygote.resources.watcher_common import setup_watchdog_for_files

# -- Logger tests --


def test_logger_creates_parent_directory(tmp_path: Path) -> None:
    log_file = tmp_path / "subdir" / "nested" / "test.log"
    Logger(log_file)
    assert log_file.parent.exists()


def test_logger_info_writes_to_file(tmp_path: Path) -> None:
    log_file = tmp_path / "test.log"
    log = Logger(log_file)
    log.info("hello world")
    content = log_file.read_text()
    assert "hello world" in content


def test_logger_info_includes_timestamp(tmp_path: Path) -> None:
    log_file = tmp_path / "test.log"
    log = Logger(log_file)
    log.info("timestamped")
    content = log_file.read_text()
    # Timestamps look like [2026-01-01T00:00:00.000000000Z]
    assert content.startswith("[")
    assert "T" in content
    assert "Z]" in content


def test_logger_debug_writes_to_file_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log_file = tmp_path / "test.log"
    log = Logger(log_file)
    log.debug("debug message")
    content = log_file.read_text()
    assert "debug message" in content
    assert "[debug]" in content
    captured = capsys.readouterr()
    assert "debug message" not in captured.out


def test_logger_info_prints_to_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log_file = tmp_path / "test.log"
    log = Logger(log_file)
    log.info("stdout message")
    captured = capsys.readouterr()
    assert "stdout message" in captured.out


def test_logger_info_appends_to_file(tmp_path: Path) -> None:
    log_file = tmp_path / "test.log"
    log = Logger(log_file)
    log.info("line 1")
    log.info("line 2")
    lines = log_file.read_text().strip().split("\n")
    assert len(lines) == 2
    assert "line 1" in lines[0]
    assert "line 2" in lines[1]


def test_logger_handles_unwritable_log_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Logger should not crash when the log file cannot be written."""
    log_file = tmp_path / "test.log"
    log = Logger(log_file)
    # Make the log directory read-only so writes fail
    log_file.write_text("")
    log_file.chmod(0o000)
    # Should not raise
    log.info("should not crash")
    log.debug("should not crash either")
    captured = capsys.readouterr()
    assert "should not crash" in captured.out
    # Restore permissions for cleanup
    log_file.chmod(0o644)


# -- require_env tests --


def test_require_env_returns_value_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_WATCHER_VAR", "hello")
    assert require_env("TEST_WATCHER_VAR") == "hello"


def test_require_env_exits_when_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_WATCHER_MISSING", raising=False)
    with pytest.raises(SystemExit):
        require_env("TEST_WATCHER_MISSING")


def test_require_env_exits_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_WATCHER_EMPTY", "")
    with pytest.raises(SystemExit):
        require_env("TEST_WATCHER_EMPTY")


# -- mtime_poll_files tests --


def test_mtime_poll_files_returns_false_when_no_files(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    cache: dict[str, tuple[float, int]] = {}
    assert not mtime_poll_files([], cache, log)


def test_mtime_poll_files_detects_new_file(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    cache: dict[str, tuple[float, int]] = {}
    test_file = tmp_path / "data.txt"

    # No file yet -- should return False
    assert not mtime_poll_files([test_file], cache, log)

    # Create the file -- should return True
    test_file.write_text("content")
    assert mtime_poll_files([test_file], cache, log)

    # No change -- should return False
    assert not mtime_poll_files([test_file], cache, log)


def test_mtime_poll_files_detects_modification(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    cache: dict[str, tuple[float, int]] = {}
    test_file = tmp_path / "data.txt"
    test_file.write_text("original")

    mtime_poll_files([test_file], cache, log)

    time.sleep(0.05)
    test_file.write_text("modified content")
    assert mtime_poll_files([test_file], cache, log)


def test_mtime_poll_files_detects_removal(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    cache: dict[str, tuple[float, int]] = {}
    test_file = tmp_path / "data.txt"
    test_file.write_text("content")

    mtime_poll_files([test_file], cache, log)
    test_file.unlink()

    assert mtime_poll_files([test_file], cache, log)


def test_mtime_poll_files_tracks_multiple_files(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    cache: dict[str, tuple[float, int]] = {}
    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"
    file_a.write_text("a")
    file_b.write_text("b")

    mtime_poll_files([file_a, file_b], cache, log)
    assert len(cache) == 2

    time.sleep(0.05)
    file_a.write_text("a modified")
    assert mtime_poll_files([file_a, file_b], cache, log)


# -- mtime_poll_directories tests --


def test_mtime_poll_directories_returns_false_for_empty_dir(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    cache: dict[str, tuple[float, int]] = {}
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    assert not mtime_poll_directories([source_dir], cache, log)


def test_mtime_poll_directories_detects_new_file(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    cache: dict[str, tuple[float, int]] = {}
    source_dir = tmp_path / "source"
    source_dir.mkdir()

    assert not mtime_poll_directories([source_dir], cache, log)

    (source_dir / "events.jsonl").write_text('{"test": true}\n')
    assert mtime_poll_directories([source_dir], cache, log)


def test_mtime_poll_directories_detects_modification(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    cache: dict[str, tuple[float, int]] = {}
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    events_file = source_dir / "events.jsonl"
    events_file.write_text('{"line": 1}\n')

    mtime_poll_directories([source_dir], cache, log)

    time.sleep(0.05)
    with events_file.open("a") as f:
        f.write('{"line": 2}\n')
    assert mtime_poll_directories([source_dir], cache, log)


def test_mtime_poll_directories_detects_removal(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    cache: dict[str, tuple[float, int]] = {}
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    events_file = source_dir / "events.jsonl"
    events_file.write_text('{"line": 1}\n')

    mtime_poll_directories([source_dir], cache, log)
    events_file.unlink()

    assert mtime_poll_directories([source_dir], cache, log)


def test_mtime_poll_directories_skips_nonexistent_directory(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    cache: dict[str, tuple[float, int]] = {}
    nonexistent = tmp_path / "does_not_exist"
    assert not mtime_poll_directories([nonexistent], cache, log)


def test_mtime_poll_directories_returns_false_when_unchanged(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    cache: dict[str, tuple[float, int]] = {}
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "events.jsonl").write_text('{"line": 1}\n')

    mtime_poll_directories([source_dir], cache, log)
    assert not mtime_poll_directories([source_dir], cache, log)


def test_mtime_poll_directories_handles_multiple_directories(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    cache: dict[str, tuple[float, int]] = {}
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "events.jsonl").write_text("a")
    (dir_b / "events.jsonl").write_text("b")

    mtime_poll_directories([dir_a, dir_b], cache, log)
    assert len(cache) == 2


# -- ChangeHandler tests --


def test_change_handler_sets_wake_event() -> None:
    wake_event = threading.Event()
    handler = ChangeHandler(wake_event)
    assert not wake_event.is_set()
    handler.on_any_event(cast(Any, None))
    assert wake_event.is_set()


# -- setup_watchdog_for_directories tests --


def test_setup_watchdog_for_directories_returns_active(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    wake_event = threading.Event()
    source_dir = tmp_path / "source"
    source_dir.mkdir()

    observer, is_active = setup_watchdog_for_directories([source_dir], wake_event, log)
    try:
        assert is_active
    finally:
        observer.stop()
        observer.join(timeout=5)


def test_setup_watchdog_for_directories_detects_changes(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    wake_event = threading.Event()
    source_dir = tmp_path / "source"
    source_dir.mkdir()

    observer, is_active = setup_watchdog_for_directories([source_dir], wake_event, log)
    try:
        assert is_active
        (source_dir / "test.txt").write_text("trigger")
        # Wait for watchdog to detect the change
        assert wake_event.wait(timeout=5)
    finally:
        observer.stop()
        observer.join(timeout=5)


# -- setup_watchdog_for_files tests --


def test_setup_watchdog_for_files_returns_active(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    wake_event = threading.Event()
    test_file = tmp_path / "watched.txt"

    observer, is_active = setup_watchdog_for_files([test_file], wake_event, log)
    try:
        assert is_active
    finally:
        observer.stop()
        observer.join(timeout=5)


def test_setup_watchdog_for_files_creates_parent_directory(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    wake_event = threading.Event()
    test_file = tmp_path / "nested" / "dir" / "watched.txt"

    observer, is_active = setup_watchdog_for_files([test_file], wake_event, log)
    try:
        assert is_active
        assert test_file.parent.exists()
    finally:
        observer.stop()
        observer.join(timeout=5)


def test_setup_watchdog_for_files_deduplicates_parent_directories(tmp_path: Path) -> None:
    log = Logger(tmp_path / "test.log")
    wake_event = threading.Event()
    file_a = tmp_path / "dir" / "a.txt"
    file_b = tmp_path / "dir" / "b.txt"
    file_a.parent.mkdir(parents=True, exist_ok=True)

    observer, is_active = setup_watchdog_for_files([file_a, file_b], wake_event, log)
    try:
        assert is_active
    finally:
        observer.stop()
        observer.join(timeout=5)
