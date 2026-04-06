import threading
from pathlib import Path

import pytest

from imbue.mngr.cli.list import _poll_events_file_for_changes
from imbue.mngr.cli.list import _run_event_driven_watch
from imbue.mngr.utils.polling import poll_until

# === Watch mode (event-driven) tests ===


def test_poll_events_file_detects_size_change(tmp_path: Path) -> None:
    """_poll_events_file_for_changes should set the changed flag when the file grows."""
    events_path = tmp_path / "events.jsonl"
    events_path.write_text('{"type":"test"}\n')
    initial_size = events_path.stat().st_size

    changed_flag = threading.Event()
    stop_event = threading.Event()

    # Start polling in a background thread
    poller = threading.Thread(
        target=_poll_events_file_for_changes,
        args=(events_path, initial_size, changed_flag, stop_event, 100),
        daemon=True,
    )
    poller.start()

    # Append new content to trigger the change
    with open(events_path, "a") as f:
        f.write('{"type":"new"}\n')

    poll_until(lambda: changed_flag.is_set(), timeout=5.0)
    stop_event.set()
    poller.join(timeout=2.0)

    assert changed_flag.is_set()


def test_poll_events_file_respects_stop_event(tmp_path: Path) -> None:
    """_poll_events_file_for_changes should return when stop_event is set."""
    events_path = tmp_path / "events.jsonl"
    events_path.write_text('{"type":"test"}\n')
    initial_size = events_path.stat().st_size

    changed_flag = threading.Event()
    stop_event = threading.Event()

    poller = threading.Thread(
        target=_poll_events_file_for_changes,
        args=(events_path, initial_size, changed_flag, stop_event, 1000),
        daemon=True,
    )
    poller.start()

    # Stop the poller without changing the file
    stop_event.set()
    poller.join(timeout=5.0)

    assert not changed_flag.is_set()
    assert not poller.is_alive()


@pytest.mark.timeout(10)
def test_run_event_driven_watch_calls_on_refresh_when_file_changes(tmp_path: Path) -> None:
    """_run_event_driven_watch should call on_refresh when the events file changes."""
    events_path = tmp_path / "events.jsonl"
    events_path.write_text('{"type":"initial"}\n')

    stop_event = threading.Event()
    refresh_called = threading.Event()

    def on_refresh() -> None:
        refresh_called.set()
        stop_event.set()

    # Run the watch in a background thread with a short max interval
    watch_thread = threading.Thread(
        target=_run_event_driven_watch,
        args=(events_path, 2, stop_event, on_refresh),
        daemon=True,
    )
    watch_thread.start()

    # Small delay to let the watch start polling
    threading.Event().wait(timeout=0.2)

    # Append new content to trigger a refresh
    with open(events_path, "a") as f:
        f.write('{"type":"change"}\n')

    watch_thread.join(timeout=8.0)
    assert refresh_called.is_set()


def test_run_event_driven_watch_respects_stop_event(tmp_path: Path) -> None:
    """_run_event_driven_watch should exit when stop_event is set."""
    events_path = tmp_path / "events.jsonl"
    events_path.write_text('{"type":"initial"}\n')

    stop_event = threading.Event()
    refresh_count = [0]

    def on_refresh() -> None:
        refresh_count[0] += 1

    # Set stop immediately
    stop_event.set()

    _run_event_driven_watch(events_path, 60, stop_event, on_refresh)

    assert refresh_count[0] == 0
