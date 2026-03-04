import json
import sys
import threading
from io import StringIO
from pathlib import Path
from threading import Lock

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.api.discovery_events import DiscoveryEventType
from imbue.mng.api.discovery_events import emit_agent_discovered
from imbue.mng.api.discovery_events import get_discovery_events_path
from imbue.mng.api.discovery_events import make_agent_discovery_event
from imbue.mng.cli.list import _poll_events_file_for_changes
from imbue.mng.cli.list import _run_event_driven_watch
from imbue.mng.cli.list import _stream_emit_line
from imbue.mng.cli.list import _stream_tail_events_file
from imbue.mng.cli.list import list_command
from imbue.mng.config.data_types import MngConfig
from imbue.mng.utils.polling import poll_until
from imbue.mng.utils.testing import make_test_discovered_agent


def test_stream_emit_line_emits_valid_json_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    emitted_ids: set[str] = set()
    lock = Lock()
    event = make_agent_discovery_event(make_test_discovered_agent())
    line = json.dumps(event.model_dump(mode="json"))

    _stream_emit_line(line, emitted_ids, lock)

    captured = capsys.readouterr()
    assert captured.out.strip()
    parsed = json.loads(captured.out.strip())
    assert parsed["type"] == DiscoveryEventType.AGENT_DISCOVERED


def test_stream_emit_line_deduplicates_by_event_id(capsys: pytest.CaptureFixture[str]) -> None:
    emitted_ids: set[str] = set()
    lock = Lock()
    event = make_agent_discovery_event(make_test_discovered_agent())
    line = json.dumps(event.model_dump(mode="json"))

    # Emit the same event twice
    _stream_emit_line(line, emitted_ids, lock)
    _stream_emit_line(line, emitted_ids, lock)

    captured = capsys.readouterr()
    # Only one line should be emitted
    output_lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(output_lines) == 1


def test_stream_emit_line_skips_empty_lines(capsys: pytest.CaptureFixture[str]) -> None:
    emitted_ids: set[str] = set()
    lock = Lock()

    _stream_emit_line("", emitted_ids, lock)
    _stream_emit_line("   ", emitted_ids, lock)

    captured = capsys.readouterr()
    assert captured.out == ""


def test_stream_emit_line_skips_invalid_json(capsys: pytest.CaptureFixture[str]) -> None:
    emitted_ids: set[str] = set()
    lock = Lock()

    _stream_emit_line("{invalid json}", emitted_ids, lock)

    captured = capsys.readouterr()
    assert captured.out == ""


def test_stream_tail_detects_new_content(temp_config: MngConfig) -> None:
    events_path = get_discovery_events_path(temp_config)

    # Write an initial event
    emit_agent_discovered(temp_config, make_test_discovered_agent())
    initial_offset = events_path.stat().st_size

    emitted_ids: set[str] = set()
    lock = Lock()
    stop_event = threading.Event()

    # Capture output by replacing stdout temporarily
    original_stdout = sys.stdout
    captured_output = StringIO()
    sys.stdout = captured_output

    try:
        # Start tail thread
        tail = threading.Thread(
            target=_stream_tail_events_file,
            args=(events_path, initial_offset, stop_event, emitted_ids, lock),
            daemon=True,
        )
        tail.start()

        # Write a new event while the tail is running
        emit_agent_discovered(temp_config, make_test_discovered_agent())

        # Poll until the tail thread picks up the new event
        poll_until(lambda: len(captured_output.getvalue().strip().splitlines()) >= 1, timeout=5.0)

        stop_event.set()
        tail.join(timeout=5.0)
    finally:
        sys.stdout = original_stdout

    # The tail should have picked up the new event
    output = captured_output.getvalue()
    output_lines = [ln for ln in output.splitlines() if ln.strip()]
    assert len(output_lines) == 1


# === CLI validation tests ===


def test_stream_with_running_filter_raises_usage_error(
    cli_runner: CliRunner, plugin_manager: pluggy.PluginManager
) -> None:
    result = cli_runner.invoke(list_command, ["--stream", "--running"], obj=plugin_manager)
    assert result.exit_code != 0


def test_stream_with_include_raises_usage_error(cli_runner: CliRunner, plugin_manager: pluggy.PluginManager) -> None:
    result = cli_runner.invoke(list_command, ["--stream", "--include", "state == 'RUNNING'"], obj=plugin_manager)
    assert result.exit_code != 0


def test_stream_with_watch_raises_usage_error(cli_runner: CliRunner, plugin_manager: pluggy.PluginManager) -> None:
    result = cli_runner.invoke(list_command, ["--stream", "--watch", "5"], obj=plugin_manager)
    assert result.exit_code != 0


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
