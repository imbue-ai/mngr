"""Tests for logging utilities."""

import io
import json
import sys
from pathlib import Path

from loguru import logger

import imbue.mng.utils.logging as mng_logging_module
from imbue.imbue_common.logging import _format_arg_value
from imbue.imbue_common.logging import log_call
from imbue.mng.config.data_types import MngContext
from imbue.mng.primitives import LogLevel
from imbue.mng.utils.logging import BUILD_COLOR
from imbue.mng.utils.logging import BufferedMessage
from imbue.mng.utils.logging import DEBUG_COLOR
from imbue.mng.utils.logging import ERROR_COLOR
from imbue.mng.utils.logging import LoggingConfig
from imbue.mng.utils.logging import LoggingSuppressor
from imbue.mng.utils.logging import RESET_COLOR
from imbue.mng.utils.logging import WARNING_COLOR
from imbue.mng.utils.logging import _format_user_message
from imbue.mng.utils.logging import _resolve_log_dir
from imbue.mng.utils.logging import remove_console_handlers
from imbue.mng.utils.logging import setup_logging


def test_resolve_log_dir_uses_absolute_path(mng_test_prefix: str) -> None:
    """Absolute log_dir should be used as-is."""
    resolved = _resolve_log_dir(Path("/absolute/path/logs"), Path("/custom/mng"))

    assert resolved == Path("/absolute/path/logs")


def test_resolve_log_dir_uses_default_host_dir_for_relative(mng_test_prefix: str) -> None:
    """Relative log_dir should be resolved relative to default_host_dir."""
    resolved = _resolve_log_dir(Path("my_logs"), Path("/custom/mng"))

    assert resolved == Path("/custom/mng/my_logs")


def test_setup_logging_creates_log_dir(temp_mng_ctx: MngContext) -> None:
    """setup_logging should create the log directory if it doesn't exist."""
    log_dir = temp_mng_ctx.config.default_host_dir / temp_mng_ctx.config.logging.log_dir
    assert not log_dir.exists()

    logging_config = LoggingConfig(console_level=LogLevel.INFO)

    setup_logging(logging_config, default_host_dir=temp_mng_ctx.config.default_host_dir, command="test")

    # The source subdirectory should be created
    source_dir = log_dir / "mng"
    assert source_dir.exists()
    assert source_dir.is_dir()


def test_setup_logging_creates_events_jsonl_file(temp_mng_ctx: MngContext) -> None:
    """setup_logging should create an events.jsonl file in the source directory."""
    log_dir = temp_mng_ctx.config.default_host_dir / temp_mng_ctx.config.logging.log_dir
    logging_config = LoggingConfig(console_level=LogLevel.INFO)

    setup_logging(logging_config, default_host_dir=temp_mng_ctx.config.default_host_dir, command="test")

    # Log a message to trigger file creation
    logger.info("test log message")

    events_file = log_dir / "mng" / "events.jsonl"
    assert events_file.exists()


def test_setup_logging_writes_flat_jsonl_with_envelope_and_loguru_fields(temp_mng_ctx: MngContext) -> None:
    """setup_logging should write flat JSON log lines with envelope fields and loguru metadata."""
    log_dir = temp_mng_ctx.config.default_host_dir / temp_mng_ctx.config.logging.log_dir
    logging_config = LoggingConfig(console_level=LogLevel.INFO)

    setup_logging(logging_config, default_host_dir=temp_mng_ctx.config.default_host_dir, command="list")

    logger.info("Listed 3 agents")

    events_file = log_dir / "mng" / "events.jsonl"
    content = events_file.read_text().strip()
    assert content, "events.jsonl should not be empty"

    # Parse the last line as flat JSON
    last_line = content.split("\n")[-1]
    parsed = json.loads(last_line)

    # Verify envelope fields at top level (same as bash logs)
    assert "timestamp" in parsed
    assert parsed["type"] == "mng"
    assert parsed["event_id"].startswith("evt-")
    assert parsed["source"] == "mng"
    assert parsed["level"] == "INFO"
    assert parsed["message"] == "Listed 3 agents"
    assert "pid" in parsed
    assert parsed["command"] == "list"

    # Verify flattened loguru metadata
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


def test_setup_logging_uses_custom_log_file_path(tmp_path: Path, temp_mng_ctx: MngContext) -> None:
    """setup_logging should create log file at custom path when log_file_path is provided."""
    custom_log_path = tmp_path / "custom_log.jsonl"

    logging_config = LoggingConfig(
        console_level=LogLevel.INFO,
        log_file_path=custom_log_path,
    )

    setup_logging(logging_config, default_host_dir=temp_mng_ctx.config.default_host_dir, command="test")

    # Log a message to create the file
    logger.info("custom path test")
    assert custom_log_path.exists()


def test_setup_logging_creates_parent_dirs_for_custom_log_path(tmp_path: Path, temp_mng_ctx: MngContext) -> None:
    """setup_logging should create parent directories for custom log file path."""
    custom_log_path = tmp_path / "nested" / "dirs" / "custom_log.jsonl"

    assert not custom_log_path.parent.exists()

    logging_config = LoggingConfig(
        console_level=LogLevel.INFO,
        log_file_path=custom_log_path,
    )

    setup_logging(logging_config, default_host_dir=temp_mng_ctx.config.default_host_dir, command="test")

    assert custom_log_path.parent.exists()


def test_setup_logging_expands_user_in_custom_log_path(tmp_path: Path, temp_mng_ctx: MngContext) -> None:
    """setup_logging should expand ~ in custom log file path.

    Note: With the test isolation fixtures, ~ expands to tmp_path (the fake home).
    """
    # home_dir is tmp_path due to test isolation
    home_dir = Path.home()

    # Create a subdirectory in the fake home
    log_subdir = tmp_path / "custom_logs"
    log_subdir.mkdir()

    # Get the relative path from home
    relative_path = log_subdir.relative_to(home_dir)
    tilde_path = Path("~") / relative_path / "expanded_log.jsonl"

    logging_config = LoggingConfig(
        console_level=LogLevel.INFO,
        log_file_path=tilde_path,
    )

    setup_logging(logging_config, default_host_dir=temp_mng_ctx.config.default_host_dir, command="test")

    expanded_path = home_dir / relative_path / "expanded_log.jsonl"
    # Log something so loguru creates the file
    logger.info("expanded test")
    assert expanded_path.exists()


# =============================================================================
# Tests for _format_arg_value
# =============================================================================


def test_format_arg_value_short_value() -> None:
    """_format_arg_value should return short values unchanged."""
    result = _format_arg_value("hello")
    assert result == "'hello'"


def test_format_arg_value_truncates_long_value() -> None:
    """_format_arg_value should truncate values over 200 chars."""
    long_value = "x" * 300
    result = _format_arg_value(long_value)
    assert len(result) == 200
    assert result.endswith("...")


def test_format_arg_value_handles_complex_objects() -> None:
    """_format_arg_value should handle complex objects."""
    complex_obj = {"key": "value", "list": [1, 2, 3]}
    result = _format_arg_value(complex_obj)
    assert "key" in result
    assert "value" in result


# =============================================================================
# Tests for log_call
# =============================================================================


def test_log_call_preserves_function_name() -> None:
    """log_call decorator should preserve function name."""

    @log_call
    def my_function() -> int:
        return 42

    assert my_function.__name__ == "my_function"


def test_log_call_returns_correct_value() -> None:
    """log_call decorator should return the function's return value."""

    @log_call
    def add(a: int, b: int) -> int:
        return a + b

    result = add(3, 5)
    assert result == 8


def test_log_call_handles_kwargs() -> None:
    """log_call decorator should handle keyword arguments."""

    @log_call
    def greet(name: str, greeting: str = "Hello") -> str:
        return f"{greeting}, {name}!"

    result = greet("World", greeting="Hi")
    assert result == "Hi, World!"


# =============================================================================
# Tests for _format_user_message
# =============================================================================


def test_format_user_message_adds_warning_prefix_for_warnings() -> None:
    """_format_user_message should add colored WARNING prefix for warning level."""
    # Mock a loguru record with WARNING level
    record = {"level": type("Level", (), {"name": "WARNING"})()}

    result = _format_user_message(record)

    assert "WARNING:" in result
    assert "{message}" in result
    assert WARNING_COLOR in result
    assert RESET_COLOR in result


def test_format_user_message_returns_plain_message_for_info() -> None:
    """_format_user_message should return plain message for INFO level."""
    record = {"level": type("Level", (), {"name": "INFO"})()}

    result = _format_user_message(record)

    assert result == "{message}\n"
    assert "WARNING" not in result
    assert WARNING_COLOR not in result


def test_format_user_message_returns_blue_message_for_debug() -> None:
    """_format_user_message should return blue-colored message for DEBUG level."""
    record = {"level": type("Level", (), {"name": "DEBUG"})()}

    result = _format_user_message(record)

    assert "{message}" in result
    assert DEBUG_COLOR in result
    assert RESET_COLOR in result
    assert "WARNING" not in result


def test_format_user_message_returns_gray_message_for_build() -> None:
    """_format_user_message should return gray-colored message for BUILD level."""
    record = {"level": type("Level", (), {"name": "BUILD"})()}

    result = _format_user_message(record)

    assert "{message}" in result
    assert BUILD_COLOR in result
    assert RESET_COLOR in result
    assert "WARNING" not in result


def test_format_user_message_adds_error_prefix_for_errors() -> None:
    """_format_user_message should add colored ERROR prefix for error level."""
    record = {"level": type("Level", (), {"name": "ERROR"})()}

    result = _format_user_message(record)

    assert "ERROR:" in result
    assert "{message}" in result
    assert ERROR_COLOR in result
    assert RESET_COLOR in result
    assert "WARNING" not in result


# =============================================================================
# Tests for LoggingSuppressor
# =============================================================================


def test_logging_suppressor_initial_state() -> None:
    """LoggingSuppressor should start unsuppressed."""
    assert not LoggingSuppressor.is_suppressed()


def test_logging_suppressor_enable_sets_suppressed() -> None:
    """Enable should set suppressed state to True."""
    try:
        LoggingSuppressor.enable(LogLevel.INFO)
        assert LoggingSuppressor.is_suppressed()
    finally:
        LoggingSuppressor.disable_and_replay(clear_screen=False)


def test_logging_suppressor_disable_clears_suppressed() -> None:
    """Disable should set suppressed state to False."""
    LoggingSuppressor.enable(LogLevel.INFO)
    assert LoggingSuppressor.is_suppressed()

    LoggingSuppressor.disable_and_replay(clear_screen=False)
    assert not LoggingSuppressor.is_suppressed()


def test_logging_suppressor_buffers_messages() -> None:
    """Suppressor should buffer messages while suppression is enabled."""
    try:
        LoggingSuppressor.enable(LogLevel.INFO)

        # Log some messages
        logger.info("Test message 1")
        logger.info("Test message 2")

        # Check that messages were buffered
        buffered = LoggingSuppressor.get_buffered_messages()
        assert len(buffered) >= 2
        assert any("Test message 1" in msg.formatted_message for msg in buffered)
        assert any("Test message 2" in msg.formatted_message for msg in buffered)
    finally:
        LoggingSuppressor.disable_and_replay(clear_screen=False)


def test_logging_suppressor_respects_buffer_size() -> None:
    """Suppressor should limit buffer to specified size."""
    try:
        # Enable with small buffer
        LoggingSuppressor.enable(LogLevel.INFO, buffer_size=3)

        # Log more messages than buffer size
        for i in range(10):
            logger.info("Message {}", i)

        # Check buffer doesn't exceed limit
        buffered = LoggingSuppressor.get_buffered_messages()
        assert len(buffered) <= 3
    finally:
        LoggingSuppressor.disable_and_replay(clear_screen=False)


def test_logging_suppressor_clears_buffer_on_disable() -> None:
    """Suppressor should clear buffer after disable_and_replay."""
    LoggingSuppressor.enable(LogLevel.INFO)
    logger.info("Test message")
    assert len(LoggingSuppressor.get_buffered_messages()) >= 1

    LoggingSuppressor.disable_and_replay(clear_screen=False)
    assert len(LoggingSuppressor.get_buffered_messages()) == 0


def test_logging_suppressor_enable_is_idempotent() -> None:
    """Calling enable twice should not reset buffer."""
    try:
        LoggingSuppressor.enable(LogLevel.INFO)
        logger.info("First message")
        initial_count = len(LoggingSuppressor.get_buffered_messages())

        # Enable again (should be no-op)
        LoggingSuppressor.enable(LogLevel.INFO)
        assert len(LoggingSuppressor.get_buffered_messages()) == initial_count
    finally:
        LoggingSuppressor.disable_and_replay(clear_screen=False)


def test_logging_suppressor_disable_is_idempotent() -> None:
    """Calling disable_and_replay twice should be safe."""
    LoggingSuppressor.enable(LogLevel.INFO)
    LoggingSuppressor.disable_and_replay(clear_screen=False)

    # Second disable should not error
    LoggingSuppressor.disable_and_replay(clear_screen=False)
    assert not LoggingSuppressor.is_suppressed()


def test_buffered_message_tracks_stderr_destination() -> None:
    """BufferedMessage should track whether message goes to stderr."""
    stdout_msg = BufferedMessage(formatted_message="stdout message", is_stderr=False)
    stderr_msg = BufferedMessage(formatted_message="stderr message", is_stderr=True)

    assert not stdout_msg.is_stderr
    assert stderr_msg.is_stderr


# =============================================================================
# Tests for remove_console_handlers
# =============================================================================


def test_remove_console_handlers_clears_handler_id(temp_mng_ctx: MngContext) -> None:
    """remove_console_handlers should clear _console_handler_id."""
    logging_config = LoggingConfig(console_level=LogLevel.INFO)

    # Setup logging to populate console handler ID
    setup_logging(logging_config, default_host_dir=temp_mng_ctx.config.default_host_dir, command="test")
    assert mng_logging_module._console_handler_id is not None

    # Remove handlers
    remove_console_handlers()

    # Handler ID should be None
    assert mng_logging_module._console_handler_id is None


def test_remove_console_handlers_is_idempotent(temp_mng_ctx: MngContext) -> None:
    """Calling remove_console_handlers twice should not error."""
    logging_config = LoggingConfig(console_level=LogLevel.INFO)

    setup_logging(logging_config, default_host_dir=temp_mng_ctx.config.default_host_dir, command="test")
    remove_console_handlers()

    # Second call should not raise an error
    remove_console_handlers()


def test_remove_console_handlers_when_no_handlers_exist() -> None:
    """remove_console_handlers should not error when no handlers exist."""
    mng_logging_module._console_handler_id = None

    # Should not raise an error
    remove_console_handlers()
    assert mng_logging_module._console_handler_id is None


# =============================================================================
# Regression tests for dynamic sinks
# =============================================================================


def test_setup_logging_writes_to_current_stderr_after_stream_replacement(
    temp_mng_ctx: MngContext,
) -> None:
    """The console handler should write to the current sys.stderr, not a stale reference.

    This is a regression test for a bug where logger.add(sys.stderr) captured the
    stream object at add time. If sys.stderr was later replaced (e.g., by pytest's
    capture mechanism), the handler would write to the old (possibly closed) stream,
    causing ValueError("I/O operation on closed file").
    """
    logging_config = LoggingConfig(
        console_level=LogLevel.INFO,
    )

    setup_logging(logging_config, default_host_dir=temp_mng_ctx.config.default_host_dir, command="test")

    # Replace sys.stderr with a StringIO to simulate pytest's capture mechanism
    original_stderr = sys.stderr
    replacement_stderr = io.StringIO()
    sys.stderr = replacement_stderr

    try:
        # Log a message -- this should write to the REPLACEMENT stderr, not the original
        logger.info("dynamic sink regression test message")

        captured_output = replacement_stderr.getvalue()
        assert "dynamic sink regression test message" in captured_output
    finally:
        sys.stderr = original_stderr
