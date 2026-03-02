"""Tests for logging utilities."""

import io
import sys
from pathlib import Path

from loguru import logger

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
from imbue.mng.utils.logging import _console_handler_ids
from imbue.mng.utils.logging import _format_user_message
from imbue.mng.utils.logging import _resolve_log_dir
from imbue.mng.utils.logging import _rotate_old_logs
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


def test_rotate_old_logs_removes_oldest_files(tmp_path: Path) -> None:
    """Should remove oldest files when exceeding max_files."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    # Create 5 log files
    for i in range(5):
        log_file = log_dir / f"log{i}.json"
        log_file.write_text(f"log {i}")

    # Keep only 3 most recent
    _rotate_old_logs(log_dir, max_files=3)

    remaining = sorted(log_dir.glob("*.json"))
    assert len(remaining) == 3


def test_rotate_old_logs_keeps_all_if_under_limit(tmp_path: Path) -> None:
    """Should not remove files if under max_files."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    # Create 3 log files
    for i in range(3):
        log_file = log_dir / f"log{i}.json"
        log_file.write_text(f"log {i}")

    # Max is 10, so should keep all
    _rotate_old_logs(log_dir, max_files=10)

    remaining = list(log_dir.glob("*.json"))
    assert len(remaining) == 3


def test_rotate_old_logs_handles_nonexistent_dir() -> None:
    """Should not error when log_dir doesn't exist."""
    _rotate_old_logs(Path("/nonexistent/path"), max_files=10)


def test_setup_logging_creates_log_dir(temp_mng_ctx: MngContext) -> None:
    """setup_logging should create the log directory if it doesn't exist."""
    log_dir = temp_mng_ctx.config.default_host_dir / temp_mng_ctx.config.logging.log_dir
    assert not log_dir.exists()

    logging_config = LoggingConfig(console_level=LogLevel.INFO)

    setup_logging(logging_config, default_host_dir=temp_mng_ctx.config.default_host_dir)

    assert log_dir.exists()
    assert log_dir.is_dir()


def test_setup_logging_creates_log_file(temp_mng_ctx: MngContext) -> None:
    """setup_logging should create a log file."""
    log_dir = temp_mng_ctx.config.default_host_dir / temp_mng_ctx.config.logging.log_dir
    logging_config = LoggingConfig(console_level=LogLevel.INFO)

    setup_logging(logging_config, default_host_dir=temp_mng_ctx.config.default_host_dir)

    log_files = list(log_dir.glob("*.json"))
    assert len(log_files) >= 1


def test_setup_logging_uses_custom_log_file_path(tmp_path: Path, temp_mng_ctx: MngContext) -> None:
    """setup_logging should create log file at custom path when log_file_path is provided."""
    custom_log_path = tmp_path / "custom_log.json"

    logging_config = LoggingConfig(
        console_level=LogLevel.INFO,
        log_file_path=custom_log_path,
    )

    setup_logging(logging_config, default_host_dir=temp_mng_ctx.config.default_host_dir)

    assert custom_log_path.exists()


def test_setup_logging_creates_parent_dirs_for_custom_log_path(tmp_path: Path, temp_mng_ctx: MngContext) -> None:
    """setup_logging should create parent directories for custom log file path."""
    custom_log_path = tmp_path / "nested" / "dirs" / "custom_log.json"

    assert not custom_log_path.parent.exists()

    logging_config = LoggingConfig(
        console_level=LogLevel.INFO,
        log_file_path=custom_log_path,
    )

    setup_logging(logging_config, default_host_dir=temp_mng_ctx.config.default_host_dir)

    assert custom_log_path.parent.exists()
    assert custom_log_path.exists()


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
    tilde_path = Path("~") / relative_path / "expanded_log.json"

    logging_config = LoggingConfig(
        console_level=LogLevel.INFO,
        log_file_path=tilde_path,
    )

    setup_logging(logging_config, default_host_dir=temp_mng_ctx.config.default_host_dir)

    expanded_path = home_dir / relative_path / "expanded_log.json"
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
        LoggingSuppressor.enable(LogLevel.INFO, LogLevel.NONE)
        assert LoggingSuppressor.is_suppressed()
    finally:
        LoggingSuppressor.disable_and_replay(clear_screen=False)


def test_logging_suppressor_disable_clears_suppressed() -> None:
    """Disable should set suppressed state to False."""
    LoggingSuppressor.enable(LogLevel.INFO, LogLevel.NONE)
    assert LoggingSuppressor.is_suppressed()

    LoggingSuppressor.disable_and_replay(clear_screen=False)
    assert not LoggingSuppressor.is_suppressed()


def test_logging_suppressor_buffers_messages() -> None:
    """Suppressor should buffer messages while suppression is enabled."""
    try:
        LoggingSuppressor.enable(LogLevel.INFO, LogLevel.NONE)

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
        LoggingSuppressor.enable(LogLevel.INFO, LogLevel.NONE, buffer_size=3)

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
    LoggingSuppressor.enable(LogLevel.INFO, LogLevel.NONE)
    logger.info("Test message")
    assert len(LoggingSuppressor.get_buffered_messages()) >= 1

    LoggingSuppressor.disable_and_replay(clear_screen=False)
    assert len(LoggingSuppressor.get_buffered_messages()) == 0


def test_logging_suppressor_enable_is_idempotent() -> None:
    """Calling enable twice should not reset buffer."""
    try:
        LoggingSuppressor.enable(LogLevel.INFO, LogLevel.NONE)
        logger.info("First message")
        initial_count = len(LoggingSuppressor.get_buffered_messages())

        # Enable again (should be no-op)
        LoggingSuppressor.enable(LogLevel.INFO, LogLevel.NONE)
        assert len(LoggingSuppressor.get_buffered_messages()) == initial_count
    finally:
        LoggingSuppressor.disable_and_replay(clear_screen=False)


def test_logging_suppressor_disable_is_idempotent() -> None:
    """Calling disable_and_replay twice should be safe."""
    LoggingSuppressor.enable(LogLevel.INFO, LogLevel.NONE)
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


def test_remove_console_handlers_clears_handler_ids(temp_mng_ctx: MngContext) -> None:
    """remove_console_handlers should clear _console_handler_ids dict."""
    logging_config = LoggingConfig(console_level=LogLevel.INFO)

    # Setup logging to populate console handler IDs
    setup_logging(logging_config, default_host_dir=temp_mng_ctx.config.default_host_dir)
    assert len(_console_handler_ids) > 0

    # Remove handlers
    remove_console_handlers()

    # Handler IDs dict should be empty
    assert len(_console_handler_ids) == 0


def test_remove_console_handlers_is_idempotent(temp_mng_ctx: MngContext) -> None:
    """Calling remove_console_handlers twice should not error."""
    logging_config = LoggingConfig(console_level=LogLevel.INFO)

    setup_logging(logging_config, default_host_dir=temp_mng_ctx.config.default_host_dir)
    remove_console_handlers()

    # Second call should not raise an error
    remove_console_handlers()
    assert len(_console_handler_ids) == 0


def test_remove_console_handlers_when_no_handlers_exist() -> None:
    """remove_console_handlers should not error when no handlers exist."""
    # Clear any existing handlers
    _console_handler_ids.clear()

    # Should not raise an error
    remove_console_handlers()
    assert len(_console_handler_ids) == 0


# =============================================================================
# Regression tests for dynamic sinks
# =============================================================================


def test_setup_logging_writes_to_current_stderr_after_stream_replacement(
    temp_mng_ctx: MngContext,
) -> None:
    """All loguru handlers should write to the current sys.stderr, not a stale reference.

    This is a regression test for a bug where logger.add(sys.stderr) captured the
    stream object at add time. If sys.stderr was later replaced (e.g., by pytest's
    capture mechanism), the handler would write to the old (possibly closed) stream,
    causing ValueError("I/O operation on closed file").

    Both the user-facing handler and the diagnostic handler write to stderr.
    """
    logging_config = LoggingConfig(
        console_level=LogLevel.INFO,
        log_level=LogLevel.DEBUG,
    )

    setup_logging(logging_config, default_host_dir=temp_mng_ctx.config.default_host_dir)

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
