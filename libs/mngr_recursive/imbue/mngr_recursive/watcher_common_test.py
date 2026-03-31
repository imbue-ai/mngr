"""Unit tests for watcher_common.py shared utilities."""

import json
import os
import threading
from pathlib import Path

import pytest
from loguru import logger
from watchdog.events import FileClosedEvent
from watchdog.events import FileClosedNoWriteEvent
from watchdog.events import FileCreatedEvent
from watchdog.events import FileDeletedEvent
from watchdog.events import FileModifiedEvent
from watchdog.events import FileMovedEvent
from watchdog.events import FileOpenedEvent

from imbue.mngr_recursive.watcher_common import ChangeHandler
from imbue.mngr_recursive.watcher_common import load_watchers_section
from imbue.mngr_recursive.watcher_common import mtime_poll_directories
from imbue.mngr_recursive.watcher_common import mtime_poll_files
from imbue.mngr_recursive.watcher_common import read_event_ids_from_jsonl
from imbue.mngr_recursive.watcher_common import require_env
from imbue.mngr_recursive.watcher_common import setup_watchdog_for_directories
from imbue.mngr_recursive.watcher_common import setup_watchdog_for_files
from imbue.mngr_recursive.watcher_common import setup_watcher_logging


def _write_minds_settings_toml(base_dir: Path, content: str) -> Path:
    """Write minds.toml in the given directory for watcher tests."""
    settings_path = base_dir / "minds.toml"
    settings_path.write_text(content)
    return settings_path


# -- setup_watcher_logging tests --


def test_setup_watcher_logging_creates_log_directory(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    setup_watcher_logging("test_watcher", log_dir)
    assert (log_dir / "test_watcher").is_dir()


def test_setup_watcher_logging_writes_jsonl_on_log(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    setup_watcher_logging("test_watcher", log_dir)
    logger.info("hello world")

    log_file = log_dir / "test_watcher" / "events.jsonl"
    assert log_file.is_file()
    content = log_file.read_text().strip()
    event = json.loads(content)
    assert event["message"] == "hello world"
    assert event["level"] == "INFO"
    assert event["source"] == "logs/test_watcher"
    assert event["type"] == "watcher"
    assert "timestamp" in event
    assert event["event_id"].startswith("evt-")


def test_setup_watcher_logging_debug_goes_to_file_and_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log_dir = tmp_path / "logs"
    setup_watcher_logging("test_watcher", log_dir)
    logger.debug("debug message")

    log_file = log_dir / "test_watcher" / "events.jsonl"
    content = log_file.read_text().strip()
    event = json.loads(content)
    assert event["message"] == "debug message"
    assert event["level"] == "DEBUG"

    captured = capsys.readouterr()
    assert "debug message" in captured.out


def test_setup_watcher_logging_info_goes_to_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log_dir = tmp_path / "logs"
    setup_watcher_logging("test_watcher", log_dir)
    logger.info("stdout message")

    captured = capsys.readouterr()
    assert "stdout message" in captured.out


def test_setup_watcher_logging_appends_to_file(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    setup_watcher_logging("test_watcher", log_dir)
    logger.info("line 1")
    logger.info("line 2")

    log_file = log_dir / "test_watcher" / "events.jsonl"
    lines = log_file.read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["message"] == "line 1"
    assert json.loads(lines[1])["message"] == "line 2"


# -- load_watchers_section tests --


def test_load_watchers_section_returns_empty_when_no_file(tmp_path: Path) -> None:
    assert load_watchers_section(tmp_path) == {}


def test_load_watchers_section_reads_watchers_table(tmp_path: Path) -> None:
    _write_minds_settings_toml(tmp_path, "[watchers]\nconversation_poll_interval_seconds = 15\n")
    result = load_watchers_section(tmp_path)
    assert result["conversation_poll_interval_seconds"] == 15


def test_load_watchers_section_handles_corrupt_file(tmp_path: Path) -> None:
    _write_minds_settings_toml(tmp_path, "this is not valid toml {{{")
    assert load_watchers_section(tmp_path) == {}


def test_load_watchers_section_returns_empty_for_missing_section(tmp_path: Path) -> None:
    _write_minds_settings_toml(tmp_path, "[other_section]\nkey = 1\n")
    assert load_watchers_section(tmp_path) == {}


# -- read_event_ids_from_jsonl tests --


def test_read_event_ids_from_jsonl_empty_file(tmp_path: Path) -> None:
    assert read_event_ids_from_jsonl(tmp_path / "nonexistent.jsonl") == set()


def test_read_event_ids_from_jsonl_reads_ids(tmp_path: Path) -> None:
    jsonl_file = tmp_path / "events.jsonl"
    jsonl_file.write_text(json.dumps({"event_id": "evt-1"}) + "\n" + json.dumps({"event_id": "evt-2"}) + "\n")
    assert read_event_ids_from_jsonl(jsonl_file) == {"evt-1", "evt-2"}


def test_read_event_ids_from_jsonl_handles_malformed_lines(tmp_path: Path) -> None:
    jsonl_file = tmp_path / "events.jsonl"
    jsonl_file.write_text("bad json\n" + json.dumps({"event_id": "evt-ok"}) + "\n")
    assert read_event_ids_from_jsonl(jsonl_file) == {"evt-ok"}


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
    cache: dict[str, tuple[float, int]] = {}
    assert not mtime_poll_files([], cache)


def test_mtime_poll_files_detects_new_file(tmp_path: Path) -> None:
    cache: dict[str, tuple[float, int]] = {}
    test_file = tmp_path / "data.txt"

    # No file yet -- should return False
    assert not mtime_poll_files([test_file], cache)

    # Create the file -- should return True
    test_file.write_text("content")
    assert mtime_poll_files([test_file], cache)

    # No change -- should return False
    assert not mtime_poll_files([test_file], cache)


def test_mtime_poll_files_detects_modification(tmp_path: Path) -> None:
    cache: dict[str, tuple[float, int]] = {}
    test_file = tmp_path / "data.txt"
    test_file.write_text("original")

    mtime_poll_files([test_file], cache)

    test_file.write_text("modified content")
    # Force a different mtime so the poller detects the change
    os.utime(test_file, (0, 999999999))
    assert mtime_poll_files([test_file], cache)


def test_mtime_poll_files_detects_removal(tmp_path: Path) -> None:
    cache: dict[str, tuple[float, int]] = {}
    test_file = tmp_path / "data.txt"
    test_file.write_text("content")

    mtime_poll_files([test_file], cache)
    test_file.unlink()

    assert mtime_poll_files([test_file], cache)


def test_mtime_poll_files_tracks_multiple_files(tmp_path: Path) -> None:
    cache: dict[str, tuple[float, int]] = {}
    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"
    file_a.write_text("a")
    file_b.write_text("b")

    mtime_poll_files([file_a, file_b], cache)
    assert len(cache) == 2

    file_a.write_text("a modified")
    os.utime(file_a, (0, 999999999))
    assert mtime_poll_files([file_a, file_b], cache)


# -- mtime_poll_directories tests --


def test_mtime_poll_directories_returns_false_for_empty_dir(tmp_path: Path) -> None:
    cache: dict[str, tuple[float, int]] = {}
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    assert not mtime_poll_directories([source_dir], cache)


def test_mtime_poll_directories_detects_new_file(tmp_path: Path) -> None:
    cache: dict[str, tuple[float, int]] = {}
    source_dir = tmp_path / "source"
    source_dir.mkdir()

    assert not mtime_poll_directories([source_dir], cache)

    (source_dir / "events.jsonl").write_text('{"test": true}\n')
    assert mtime_poll_directories([source_dir], cache)


def test_mtime_poll_directories_detects_modification(tmp_path: Path) -> None:
    cache: dict[str, tuple[float, int]] = {}
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    events_file = source_dir / "events.jsonl"
    events_file.write_text('{"line": 1}\n')

    mtime_poll_directories([source_dir], cache)

    with events_file.open("a") as f:
        f.write('{"line": 2}\n')
    os.utime(events_file, (0, 999999999))
    assert mtime_poll_directories([source_dir], cache)


def test_mtime_poll_directories_detects_removal(tmp_path: Path) -> None:
    cache: dict[str, tuple[float, int]] = {}
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    events_file = source_dir / "events.jsonl"
    events_file.write_text('{"line": 1}\n')

    mtime_poll_directories([source_dir], cache)
    events_file.unlink()

    assert mtime_poll_directories([source_dir], cache)


def test_mtime_poll_directories_skips_nonexistent_directory(tmp_path: Path) -> None:
    cache: dict[str, tuple[float, int]] = {}
    nonexistent = tmp_path / "does_not_exist"
    assert not mtime_poll_directories([nonexistent], cache)


def test_mtime_poll_directories_returns_false_when_unchanged(tmp_path: Path) -> None:
    cache: dict[str, tuple[float, int]] = {}
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "events.jsonl").write_text('{"line": 1}\n')

    mtime_poll_directories([source_dir], cache)
    assert not mtime_poll_directories([source_dir], cache)


def test_mtime_poll_directories_handles_multiple_directories(tmp_path: Path) -> None:
    cache: dict[str, tuple[float, int]] = {}
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "events.jsonl").write_text("a")
    (dir_b / "events.jsonl").write_text("b")

    mtime_poll_directories([dir_a, dir_b], cache)
    assert len(cache) == 2


# -- ChangeHandler tests --


def test_change_handler_sets_wake_event_on_modification() -> None:
    wake_event = threading.Event()
    handler = ChangeHandler(wake_event)
    assert not wake_event.is_set()
    handler.on_any_event(FileModifiedEvent("test.txt"))
    assert wake_event.is_set()


def test_change_handler_ignores_non_change_events() -> None:
    wake_event = threading.Event()
    handler = ChangeHandler(wake_event)

    handler.on_any_event(FileOpenedEvent("test.txt"))
    assert not wake_event.is_set()

    handler.on_any_event(FileClosedEvent("test.txt"))
    assert not wake_event.is_set()

    handler.on_any_event(FileClosedNoWriteEvent("test.txt"))
    assert not wake_event.is_set()


def test_change_handler_wakes_on_create_delete_move() -> None:
    for event_cls in (FileCreatedEvent, FileDeletedEvent):
        wake_event = threading.Event()
        handler = ChangeHandler(wake_event)
        handler.on_any_event(event_cls("test.txt"))
        assert wake_event.is_set(), f"Expected wake for {event_cls.__name__}"

    wake_event = threading.Event()
    handler = ChangeHandler(wake_event)
    handler.on_any_event(FileMovedEvent("old.txt", "new.txt"))
    assert wake_event.is_set(), "Expected wake for FileMovedEvent"


# -- setup_watchdog_for_directories tests --


def test_setup_watchdog_for_directories_returns_active(tmp_path: Path) -> None:
    wake_event = threading.Event()
    source_dir = tmp_path / "source"
    source_dir.mkdir()

    observer, is_active = setup_watchdog_for_directories([source_dir], wake_event)
    try:
        assert is_active
    finally:
        observer.stop()
        observer.join(timeout=5)


def test_setup_watchdog_for_directories_detects_changes(tmp_path: Path) -> None:
    wake_event = threading.Event()
    source_dir = tmp_path / "source"
    source_dir.mkdir()

    observer, is_active = setup_watchdog_for_directories([source_dir], wake_event)
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
    wake_event = threading.Event()
    test_file = tmp_path / "watched.txt"

    observer, is_active = setup_watchdog_for_files([test_file], wake_event)
    try:
        assert is_active
    finally:
        observer.stop()
        observer.join(timeout=5)


def test_setup_watchdog_for_files_creates_parent_directory(tmp_path: Path) -> None:
    wake_event = threading.Event()
    test_file = tmp_path / "nested" / "dir" / "watched.txt"

    observer, is_active = setup_watchdog_for_files([test_file], wake_event)
    try:
        assert is_active
        assert test_file.parent.exists()
    finally:
        observer.stop()
        observer.join(timeout=5)


def test_setup_watchdog_for_files_deduplicates_parent_directories(tmp_path: Path) -> None:
    wake_event = threading.Event()
    file_a = tmp_path / "dir" / "a.txt"
    file_b = tmp_path / "dir" / "b.txt"
    file_a.parent.mkdir(parents=True, exist_ok=True)

    observer, is_active = setup_watchdog_for_files([file_a, file_b], wake_event)
    try:
        assert is_active
    finally:
        observer.stop()
        observer.join(timeout=5)


# -- get_mngr_command tests --


def test_get_mngr_command_returns_path_when_binary_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imbue.mngr_recursive.watcher_common import get_mngr_command

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mngr_bin = bin_dir / "mngr"
    mngr_bin.write_text("#!/bin/bash\n")
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(bin_dir))

    result = get_mngr_command()
    assert result == [str(mngr_bin)]


def test_get_mngr_command_raises_when_env_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from imbue.mngr_recursive.watcher_common import MngrNotInstalledError
    from imbue.mngr_recursive.watcher_common import get_mngr_command

    monkeypatch.delenv("UV_TOOL_BIN_DIR", raising=False)
    with pytest.raises(MngrNotInstalledError, match="UV_TOOL_BIN_DIR is not set"):
        get_mngr_command()


def test_get_mngr_command_raises_when_binary_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imbue.mngr_recursive.watcher_common import MngrNotInstalledError
    from imbue.mngr_recursive.watcher_common import get_mngr_command

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(bin_dir))

    with pytest.raises(MngrNotInstalledError, match="not found"):
        get_mngr_command()


# -- _format_nanosecond_timestamp tests --


def test_format_nanosecond_timestamp_formats_utc() -> None:
    from datetime import datetime
    from datetime import timezone

    from imbue.mngr_recursive.watcher_common import _format_nanosecond_timestamp

    dt = datetime(2025, 3, 15, 10, 30, 45, 123456, tzinfo=timezone.utc)
    result = _format_nanosecond_timestamp(dt)
    assert result == "2025-03-15T10:30:45.123456000Z"


# -- _make_jsonl_file_sink tests --


def test_jsonl_file_sink_creates_file_and_writes_event(tmp_path: Path) -> None:
    from imbue.mngr_recursive.watcher_common import _make_jsonl_file_sink

    log_file = tmp_path / "logs" / "events.jsonl"
    sink = _make_jsonl_file_sink(str(log_file), "test_event", "test/source")

    # Simulate calling the sink with a loguru message-like object
    import types

    msg_record = types.SimpleNamespace(
        record={
            "time": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            "level": types.SimpleNamespace(name="INFO"),
            "message": "test sink message",
        }
    )
    sink(msg_record)

    assert log_file.exists()
    content = log_file.read_text().strip()
    event = json.loads(content)
    assert event["type"] == "test_event"
    assert event["source"] == "test/source"
    assert event["message"] == "test sink message"
    assert event["level"] == "INFO"
    assert event["event_id"].startswith("evt-")


def test_jsonl_file_sink_rotates_when_max_size_exceeded(tmp_path: Path) -> None:
    from imbue.mngr_recursive.watcher_common import _make_jsonl_file_sink

    log_file = tmp_path / "events.jsonl"

    # Use a tiny max_size to trigger rotation
    sink = _make_jsonl_file_sink(str(log_file), "test", "test/source", max_size_bytes=50)

    import types
    from datetime import datetime
    from datetime import timezone

    now = datetime.now(timezone.utc)
    msg = types.SimpleNamespace(
        record={
            "time": now,
            "level": types.SimpleNamespace(name="INFO"),
            "message": "a message that is long enough to exceed 50 bytes",
        }
    )

    # Write enough to exceed the limit
    sink(msg)
    sink(msg)

    # After rotation, the original file should still exist (recreated)
    assert log_file.exists()
    # And a rotated file should exist
    rotated = log_file.with_name(f"{log_file.name}.1")
    assert rotated.exists()


# -- run_watcher_loop tests --


def test_run_watcher_loop_calls_on_tick_and_exits_on_keyboard_interrupt(tmp_path: Path) -> None:
    from imbue.mngr_recursive.watcher_common import run_watcher_loop

    tick_count = 0
    source_dir = tmp_path / "source"
    source_dir.mkdir()

    def on_tick() -> None:
        nonlocal tick_count
        tick_count += 1
        if tick_count >= 2:
            raise KeyboardInterrupt

    run_watcher_loop(
        "test_watcher",
        poll_interval=0,
        watch_targets=[source_dir],
        is_directory_mode=True,
        on_tick=on_tick,
    )

    assert tick_count >= 2


def test_run_watcher_loop_file_mode(tmp_path: Path) -> None:
    from imbue.mngr_recursive.watcher_common import run_watcher_loop

    tick_count = 0
    watch_file = tmp_path / "watched.txt"

    def on_tick() -> None:
        nonlocal tick_count
        tick_count += 1
        if tick_count >= 1:
            raise KeyboardInterrupt

    run_watcher_loop(
        "test_watcher",
        poll_interval=0,
        watch_targets=[watch_file],
        is_directory_mode=False,
        on_tick=on_tick,
    )

    assert tick_count >= 1
