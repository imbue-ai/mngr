"""Tests for logging module."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.imbue_common.logging import RotatingLineWriter
from imbue.imbue_common.logging import _build_flat_log_dict
from imbue.imbue_common.logging import _format_arg_value
from imbue.imbue_common.logging import cleanup_old_rotated_files
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from imbue.imbue_common.logging import log_call
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.logging import make_jsonl_file_sink
from imbue.imbue_common.logging import rotate_file_if_too_large
from imbue.imbue_common.logging import setup_logging
from imbue.imbue_common.logging import trace_span


class _SampleLoggingError(Exception):
    """Sample exception used to exercise logging behavior on failure paths."""


class LogCapture:
    """Captures loguru messages and levels for test assertions."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.levels: list[str] = []
        self.extras: list[dict[str, Any]] = []

    def sink(self, message: Any) -> None:
        record = message.record
        self.messages.append(record["message"])
        self.levels.append(record["level"].name)
        self.extras.append(dict(record["extra"]))


@contextmanager
def capture_logs() -> Iterator[LogCapture]:
    """Context manager that installs a loguru sink and yields a LogCapture."""
    cap = LogCapture()
    handler_id = logger.add(cap.sink, level="TRACE", format="{message}")
    try:
        yield cap
    finally:
        logger.remove(handler_id)


def test_setup_logging_does_not_raise() -> None:
    """setup_logging should configure logging without raising."""
    setup_logging()


def test_setup_logging_with_custom_level() -> None:
    """setup_logging should accept custom log levels."""
    setup_logging(level="DEBUG")
    setup_logging(level="info")


# =============================================================================
# Tests for log_span
# =============================================================================


def test_log_span_emits_debug_on_entry_and_trace_on_exit() -> None:
    """log_span should emit a debug message on entry and a trace message on exit."""
    captured_messages: list[str] = []
    captured_levels: list[str] = []

    def sink(message: Any) -> None:
        record = message.record
        captured_messages.append(record["message"])
        captured_levels.append(record["level"].name)

    handler_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        with log_span("processing items"):
            pass

        assert len(captured_messages) == 2
        assert captured_messages[0] == "processing items"
        assert captured_levels[0] == "DEBUG"
        assert "processing items [done in " in captured_messages[1]
        assert " sec]" in captured_messages[1]
        assert captured_levels[1] == "TRACE"
    finally:
        logger.remove(handler_id)


def test_log_span_passes_format_args_to_messages() -> None:
    """log_span should pass positional format args to both entry and exit messages."""
    captured_messages: list[str] = []

    def sink(message: Any) -> None:
        captured_messages.append(message.record["message"])

    handler_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        with log_span("creating agent {} on host {}", "agent-1", "host-1"):
            pass

        assert captured_messages[0] == "creating agent agent-1 on host host-1"
        assert "creating agent agent-1 on host host-1 [done in " in captured_messages[1]
    finally:
        logger.remove(handler_id)


def test_log_span_passes_context_kwargs_via_contextualize() -> None:
    """log_span should set context kwargs via logger.contextualize."""
    captured_extras: list[dict] = []

    def sink(message: Any) -> None:
        captured_extras.append(dict(message.record["extra"]))

    handler_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        with log_span("writing env vars", count=5, path="/tmp"):
            pass

        # Both entry and exit messages should have the context
        assert captured_extras[0]["count"] == 5
        assert captured_extras[0]["path"] == "/tmp"
        assert captured_extras[1]["count"] == 5
        assert captured_extras[1]["path"] == "/tmp"
    finally:
        logger.remove(handler_id)


def test_log_span_measures_elapsed_time() -> None:
    """log_span should include a non-negative elapsed time in the exit message."""
    captured_messages: list[str] = []

    def sink(message: Any) -> None:
        captured_messages.append(message.record["message"])

    handler_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        with log_span("doing work"):
            # Do some trivial computation to take a non-zero amount of time
            _result = sum(range(1000))

        # Extract the timing from the trace message
        trace_message = captured_messages[1]
        assert "[done in " in trace_message
        timing_str = trace_message.split("[done in ")[1].split(" sec]")[0]
        elapsed = float(timing_str)
        assert elapsed >= 0.0
        assert elapsed < 1.0
    finally:
        logger.remove(handler_id)


def test_log_span_logs_timing_even_on_exception() -> None:
    """log_span should emit the trace message even when an exception occurs."""
    captured_messages: list[str] = []
    captured_levels: list[str] = []

    def sink(message: Any) -> None:
        captured_messages.append(message.record["message"])
        captured_levels.append(message.record["level"].name)

    handler_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        try:
            with log_span("risky operation"):
                raise _SampleLoggingError("test error")
        except _SampleLoggingError:
            pass

        assert len(captured_messages) == 2
        assert captured_messages[0] == "risky operation"
        assert captured_levels[0] == "DEBUG"
        assert "risky operation [failed after " in captured_messages[1]
        assert captured_levels[1] == "TRACE"
    finally:
        logger.remove(handler_id)


def test_log_span_context_does_not_leak_outside_span() -> None:
    """Context kwargs should not be present in log records after the span exits."""
    captured_extras: list[dict] = []

    def sink(message: Any) -> None:
        captured_extras.append(dict(message.record["extra"]))

    handler_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        with log_span("scoped operation", scope_var="inside"):
            pass

        # Log something after the span
        logger.debug("after span")

        # The message after the span should not have the context
        assert "scope_var" not in captured_extras[2]
    finally:
        logger.remove(handler_id)


# =============================================================================
# Tests for JSONL event formatting
# =============================================================================


def test_format_nanosecond_iso_timestamp_produces_correct_format() -> None:
    dt = datetime(2026, 3, 1, 12, 30, 45, 123456, tzinfo=timezone.utc)
    result = format_nanosecond_iso_timestamp(dt)
    assert result == "2026-03-01T12:30:45.123456000Z"


def test_format_nanosecond_iso_timestamp_zero_microseconds() -> None:
    dt = datetime(2026, 1, 1, 0, 0, 0, 0, tzinfo=timezone.utc)
    result = format_nanosecond_iso_timestamp(dt)
    assert result == "2026-01-01T00:00:00.000000000Z"


def test_generate_log_event_id_returns_unique_ids() -> None:
    id_a = generate_log_event_id()
    id_b = generate_log_event_id()
    assert id_a != id_b
    assert id_a.startswith("evt-")
    assert id_b.startswith("evt-")
    # Should be evt- prefix + 32 hex chars (uuid4)
    assert len(id_a) == 4 + 32


def test_build_flat_log_dict_produces_envelope_and_loguru_fields() -> None:
    """_build_flat_log_dict should produce a flat dict with all expected fields."""
    captured_dicts: list[dict[str, Any]] = []

    def sink(message: Any) -> None:
        captured_dicts.append(_build_flat_log_dict(message.record, "mngr", "mngr", "create"))

    handler_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        logger.info("Created agent {}", "test-agent")
    finally:
        logger.remove(handler_id)

    parsed = captured_dicts[0]

    # Envelope fields at top level
    assert parsed["type"] == "mngr"
    assert parsed["source"] == "mngr"
    assert parsed["command"] == "create"
    assert parsed["event_id"].startswith("evt-")
    assert "timestamp" in parsed
    assert "pid" in parsed
    assert parsed["level"] == "INFO"
    assert parsed["message"] == "Created agent test-agent"

    # Loguru metadata at top level
    assert "function" in parsed
    assert "line" in parsed
    assert "module" in parsed
    assert "logger_name" in parsed
    assert "file_name" in parsed
    assert "file_path" in parsed
    assert "elapsed_seconds" in parsed
    assert "process_name" in parsed
    assert "thread_name" in parsed
    assert "thread_id" in parsed

    # Serializable to JSON
    json_line = json.dumps(parsed, separators=(",", ":"), default=str)
    reparsed = json.loads(json_line)
    assert reparsed["message"] == "Created agent test-agent"


def test_build_flat_log_dict_omits_command_when_none() -> None:
    """When command is None, the command key should not appear in the dict."""
    captured_dicts: list[dict[str, Any]] = []

    def sink(message: Any) -> None:
        captured_dicts.append(_build_flat_log_dict(message.record, "event_watcher", "event_watcher", None))

    handler_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        logger.debug("Watching events")
    finally:
        logger.remove(handler_id)

    assert captured_dicts[0]["type"] == "event_watcher"
    assert "command" not in captured_dicts[0]


def test_build_flat_log_dict_includes_extra_context() -> None:
    """Extra context from logger.contextualize should appear in the extra field."""
    captured_dicts: list[dict[str, Any]] = []

    def sink(message: Any) -> None:
        captured_dicts.append(_build_flat_log_dict(message.record, "mngr", "mngr", "list"))

    handler_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        with logger.contextualize(host="my-host"):
            logger.info("test")
    finally:
        logger.remove(handler_id)

    assert captured_dicts[0]["extra"]["host"] == "my-host"


def test_build_flat_log_dict_handles_special_chars() -> None:
    """Messages with quotes and newlines should serialize cleanly to JSON."""
    captured_dicts: list[dict[str, Any]] = []

    def sink(message: Any) -> None:
        captured_dicts.append(_build_flat_log_dict(message.record, "mngr", "mngr", None))

    handler_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        logger.info('Path "C:\\test"\nLine 2')
    finally:
        logger.remove(handler_id)

    d = captured_dicts[0]
    assert '"C:\\test"' in d["message"]
    assert "\n" in d["message"]
    # Must round-trip through JSON
    reparsed = json.loads(json.dumps(d, default=str))
    assert reparsed["message"] == d["message"]


# =============================================================================
# Tests for _format_arg_value
# =============================================================================


def test_format_arg_value_short_value_unchanged() -> None:
    """Short values should be returned as-is."""
    assert _format_arg_value("hello") == "'hello'"


def test_format_arg_value_truncates_long_value() -> None:
    """Values longer than _MAX_LOG_VALUE_REPR_LENGTH should be truncated."""
    long_string = "x" * 300
    result = _format_arg_value(long_string)
    assert result.endswith("...")
    assert len(result) == 200


# =============================================================================
# Tests for log_call
# =============================================================================


def test_log_call_logs_function_call_and_result() -> None:
    """log_call should log the function call at debug and the result at trace."""

    @log_call
    def add(a: int, b: int) -> int:
        return a + b

    with capture_logs() as cap:
        result = add(1, 2)
        assert result == 3
        assert len(cap.messages) == 2
        assert "Calling add" in cap.messages[0]
        assert "Calling add [done in " in cap.messages[1]


def test_log_call_preserves_function_name() -> None:
    """log_call should preserve the original function name."""

    @log_call
    def my_function() -> None:
        pass

    assert my_function.__name__ == "my_function"


# =============================================================================
# Tests for trace_span
# =============================================================================


def test_trace_span_emits_trace_messages() -> None:
    """trace_span should emit trace messages on entry and exit."""
    with capture_logs() as cap:
        with trace_span("processing"):
            pass

        assert len(cap.messages) == 2
        assert cap.messages[0] == "processing"
        assert cap.levels[0] == "TRACE"
        assert "processing [done in " in cap.messages[1]
        assert cap.levels[1] == "TRACE"


def test_trace_span_disabled_skips_logging() -> None:
    """trace_span with _is_trace_span_enabled=False should not log."""
    with capture_logs() as cap:
        with trace_span("should not log", _is_trace_span_enabled=False):
            pass

        assert len(cap.messages) == 0


def test_trace_span_logs_on_exception() -> None:
    """trace_span should log failed timing on exception."""
    with capture_logs() as cap:
        try:
            with trace_span("risky"):
                raise _SampleLoggingError("boom")
        except _SampleLoggingError:
            pass

        assert len(cap.messages) == 2
        assert "risky [failed after " in cap.messages[1]


# =============================================================================
# Tests for make_jsonl_file_sink
# =============================================================================


def test_make_jsonl_file_sink_writes_json_lines(tmp_path: Path) -> None:
    """make_jsonl_file_sink should write valid JSONL to the specified file."""
    log_file = tmp_path / "test.jsonl"
    sink = make_jsonl_file_sink(
        file_path=str(log_file),
        event_type="mngr",
        event_source="test",
        command="create",
        max_size_bytes=1_000_000,
    )

    handler_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        logger.info("test message")
    finally:
        logger.remove(handler_id)

    assert log_file.exists()
    content = log_file.read_text()
    parsed = json.loads(content.strip())
    assert parsed["type"] == "mngr"
    assert parsed["source"] == "test"
    assert parsed["command"] == "create"
    assert parsed["message"] == "test message"


def test_make_jsonl_file_sink_rotates_on_size(tmp_path: Path) -> None:
    """make_jsonl_file_sink should rotate when file exceeds max_size_bytes."""
    log_file = tmp_path / "events.jsonl"
    sink = make_jsonl_file_sink(
        file_path=str(log_file),
        event_type="mngr",
        event_source="test",
        command=None,
        max_size_bytes=100,
    )

    handler_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        for i in range(5):
            logger.info("message number {}", i)
    finally:
        logger.remove(handler_id)

    # Should have rotated files with timestamp-based names (events.jsonl.TIMESTAMP)
    rotated_files = [f for f in tmp_path.iterdir() if f.name.startswith("events.jsonl.") and f.name != "events.jsonl"]
    assert len(rotated_files) >= 1


def test_make_jsonl_file_sink_cleans_up_rotated_files_for_custom_filename(tmp_path: Path) -> None:
    """Rotated copies of a non-``events.jsonl`` file must be pruned, not leaked.

    Regression guard: cleanup used to be hard-coded to the ``events.jsonl``
    rotated-file pattern, so a custom ``--log-file`` name accumulated rotated
    copies forever.
    """
    log_file = tmp_path / "latchkey_forward_events.jsonl"
    sink = make_jsonl_file_sink(
        file_path=str(log_file),
        event_type="mngr",
        event_source="test",
        command=None,
        max_size_bytes=100,
        max_rotated_count=2,
    )

    handler_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        for i in range(40):
            logger.info("message number {}", i)
    finally:
        logger.remove(handler_id)

    rotated = [f for f in tmp_path.iterdir() if f.name.startswith("latchkey_forward_events.jsonl.")]
    assert 0 < len(rotated) <= 2


# =============================================================================
# Tests for cleanup_old_rotated_files / rotate_file_if_too_large
# =============================================================================


def test_cleanup_old_rotated_files_respects_custom_base_name(tmp_path: Path) -> None:
    """Only rotated copies of the given base name are pruned; others are left alone."""
    for ts in ("20240101000000000001", "20240101000000000002", "20240101000000000003"):
        (tmp_path / f"gateway.log.{ts}").write_text("x")
    # A different base name and the live file must survive.
    (tmp_path / "gateway.log").write_text("live")
    (tmp_path / "events.jsonl.20240101000000000009").write_text("other")

    cleanup_old_rotated_files(tmp_path, max_rotated_count=1, base_name="gateway.log")

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == [
        "events.jsonl.20240101000000000009",
        "gateway.log",
        "gateway.log.20240101000000000003",
    ]


def test_rotate_file_if_too_large_rotates_when_over_threshold(tmp_path: Path) -> None:
    path = tmp_path / "raw.log"
    path.write_bytes(b"a" * 500)

    assert rotate_file_if_too_large(path, max_size_bytes=100) is True
    rotated = [p for p in tmp_path.iterdir() if p.name.startswith("raw.log.")]
    assert len(rotated) == 1
    assert rotated[0].stat().st_size == 500
    assert not path.exists()


def test_rotate_file_if_too_large_is_noop_when_small_or_absent(tmp_path: Path) -> None:
    path = tmp_path / "raw.log"
    path.write_bytes(b"a" * 50)
    assert rotate_file_if_too_large(path, max_size_bytes=100) is False
    assert path.stat().st_size == 50

    missing = tmp_path / "missing.log"
    assert rotate_file_if_too_large(missing, max_size_bytes=100) is False


# =============================================================================
# Tests for RotatingLineWriter
# =============================================================================


def test_rotating_line_writer_timestamps_and_appends_lines(tmp_path: Path) -> None:
    log_file = tmp_path / "gateway.log"
    writer = RotatingLineWriter(path=log_file, max_size_bytes=1_000_000)
    writer.write_line("first line")
    # The trailing newline is normalized so exactly one is written.
    writer.write_line("second line\n")
    writer.close()

    lines = log_file.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith(" first line")
    assert lines[1].endswith(" second line")
    # Each line is prefixed with a parseable ISO-8601 UTC nanosecond timestamp.
    stamp = lines[0].split(" ", 1)[0]
    assert stamp.endswith("Z")
    datetime.strptime(stamp[:26], "%Y-%m-%dT%H:%M:%S.%f")


def test_rotating_line_writer_can_disable_timestamps(tmp_path: Path) -> None:
    log_file = tmp_path / "raw.log"
    writer = RotatingLineWriter(path=log_file, is_timestamped=False)
    writer.write_line("plain line")
    writer.close()
    assert log_file.read_text() == "plain line\n"


def test_rotating_line_writer_rotates_and_prunes_by_size(tmp_path: Path) -> None:
    log_file = tmp_path / "gateway.log"
    writer = RotatingLineWriter(
        path=log_file,
        max_size_bytes=200,
        max_rotated_count=2,
        is_timestamped=False,
    )
    for i in range(100):
        writer.write_line(f"line {i} with some padding text")
    writer.close()

    rotated = [p for p in tmp_path.iterdir() if p.name.startswith("gateway.log.")]
    # Rotation happened and pruning kept at most max_rotated_count copies.
    assert 0 < len(rotated) <= 2
    assert log_file.exists()


def test_rotating_line_writer_opens_lazily(tmp_path: Path) -> None:
    """Constructing a writer must not create the file until a line is written."""
    log_file = tmp_path / "gateway.log"
    writer = RotatingLineWriter(path=log_file)
    assert not log_file.exists()
    writer.write_line("now it exists")
    writer.close()
    assert log_file.is_file()


# =============================================================================
# Tests for _build_flat_log_dict exception info
# =============================================================================


def test_build_flat_log_dict_includes_exception_info() -> None:
    """_build_flat_log_dict should include exception info when an exception is logged."""
    captured_dicts: list[dict[str, Any]] = []

    def sink(message: Any) -> None:
        captured_dicts.append(_build_flat_log_dict(message.record, "mngr", "mngr", None))

    handler_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        try:
            raise ValueError("test error")
        except ValueError as e:
            logger.opt(exception=e).error("Something failed")
    finally:
        logger.remove(handler_id)

    exc_info = captured_dicts[0]["exception"]
    assert exc_info is not None
    assert exc_info["type"] == "ValueError"
    assert "test error" in exc_info["value"]
    assert exc_info["traceback"] is True
