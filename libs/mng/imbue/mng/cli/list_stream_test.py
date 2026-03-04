import json
import sys
import threading
from io import StringIO
from threading import Lock

import pytest

from imbue.mng.api.discovery_events import DiscoveryEventType
from imbue.mng.api.discovery_events import emit_agent_discovered
from imbue.mng.api.discovery_events import get_discovery_events_path
from imbue.mng.api.discovery_events import make_agent_discovery_event
from imbue.mng.cli.list import _stream_emit_line
from imbue.mng.cli.list import _stream_tail_events_file
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
