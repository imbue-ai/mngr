import threading
from pathlib import Path
from uuid import uuid4

from imbue.mngr.utils.file_watch import DirectoryWatchGroup
from imbue.mngr.utils.file_watch import start_event_forwarder


def test_directory_watch_group_wakes_on_file_change(tmp_path: Path) -> None:
    watch_group = DirectoryWatchGroup()
    wake_event = threading.Event()
    try:
        assert watch_group.watch(tmp_path, wake_event) is True

        (tmp_path / f"events-{uuid4().hex}.jsonl").write_text("line\n")

        assert wake_event.wait(timeout=5.0) is True
    finally:
        watch_group.stop()


def test_directory_watch_group_watch_missing_directory_returns_false(tmp_path: Path) -> None:
    watch_group = DirectoryWatchGroup()
    try:
        is_watching = watch_group.watch(tmp_path / f"missing-{uuid4().hex}", threading.Event())
        assert is_watching is False
    finally:
        watch_group.stop()


def test_directory_watch_group_unwatched_directory_no_longer_wakes(tmp_path: Path) -> None:
    watch_group = DirectoryWatchGroup()
    wake_event = threading.Event()
    try:
        assert watch_group.watch(tmp_path, wake_event) is True
        watch_group.unwatch(tmp_path)

        (tmp_path / f"events-{uuid4().hex}.jsonl").write_text("line\n")

        # Bounded negative check: the unwatched directory's event stays clear.
        assert wake_event.wait(timeout=0.5) is False
    finally:
        watch_group.stop()


def test_directory_watch_group_wake_all_sets_registered_events(tmp_path: Path) -> None:
    watch_group = DirectoryWatchGroup()
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first_wake = threading.Event()
    second_wake = threading.Event()
    try:
        assert watch_group.watch(first_dir, first_wake) is True
        assert watch_group.watch(second_dir, second_wake) is True

        watch_group.wake_all()

        assert first_wake.is_set()
        assert second_wake.is_set()
    finally:
        watch_group.stop()


def test_directory_watch_group_watch_after_stop_returns_false(tmp_path: Path) -> None:
    watch_group = DirectoryWatchGroup()
    watch_group.stop()

    assert watch_group.watch(tmp_path, threading.Event()) is False


def test_start_event_forwarder_sets_target_when_source_fires() -> None:
    source_event = threading.Event()
    target_event = threading.Event()
    start_event_forwarder(source_event, target_event, name=f"test-forwarder-{uuid4().hex}")

    assert target_event.is_set() is False
    source_event.set()

    assert target_event.wait(timeout=5.0) is True
