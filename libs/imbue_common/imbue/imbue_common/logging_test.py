"""Tests for logging module."""

import json
from datetime import datetime
from datetime import timezone
from typing import Any

from loguru import logger

from imbue.imbue_common.logging import _build_flat_log_dict
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.logging import setup_logging
from imbue.mng.errors import BaseMngError


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
                raise BaseMngError("test error")
        except BaseMngError:
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
        captured_dicts.append(_build_flat_log_dict(message.record, "mng", "mng", "create"))

    handler_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        logger.info("Created agent {}", "test-agent")
    finally:
        logger.remove(handler_id)

    parsed = captured_dicts[0]

    # Envelope fields at top level
    assert parsed["type"] == "mng"
    assert parsed["source"] == "mng"
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
        captured_dicts.append(_build_flat_log_dict(message.record, "mng", "mng", "list"))

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
        captured_dicts.append(_build_flat_log_dict(message.record, "mng", "mng", None))

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
