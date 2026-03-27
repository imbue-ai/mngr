"""Tests for logging utilities."""

import io
import json
import logging
import sys
import threading
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import cast

import paramiko.channel
import pytest
from loguru import logger

import imbue.mngr.utils.logging as mngr_logging_module
from imbue.imbue_common.logging import _format_arg_value
from imbue.imbue_common.logging import log_call
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import LogLevel
from imbue.mngr.utils.logging import BUILD_COLOR
from imbue.mngr.utils.logging import BufferedMessage
from imbue.mngr.utils.logging import DEBUG_COLOR
from imbue.mngr.utils.logging import ERROR_COLOR
from imbue.mngr.utils.logging import LoggingConfig
from imbue.mngr.utils.logging import LoggingSuppressor
from imbue.mngr.utils.logging import RESET_COLOR
from imbue.mngr.utils.logging import WARNING_COLOR
from imbue.mngr.utils.logging import _ParamikoToLoguruHandler
from imbue.mngr.utils.logging import _format_user_message
from imbue.mngr.utils.logging import _is_expected_paramiko_thread_exception
from imbue.mngr.utils.logging import _patched_transport_log
from imbue.mngr.utils.logging import _resolve_log_dir
from imbue.mngr.utils.logging import _threading_excepthook
from imbue.mngr.utils.logging import remove_console_handlers
from imbue.mngr.utils.logging import setup_logging
from imbue.mngr.utils.logging import suppress_warnings


def test_resolve_log_dir_uses_absolute_path(mngr_test_prefix: str) -> None:
    """Absolute log_dir should be used as-is."""
    resolved = _resolve_log_dir(Path("/absolute/path/logs"), Path("/custom/mngr"))

    assert resolved == Path("/absolute/path/logs")


def test_resolve_log_dir_uses_default_host_dir_for_relative(mngr_test_prefix: str) -> None:
    """Relative log_dir should be resolved relative to default_host_dir."""
    resolved = _resolve_log_dir(Path("my_logs"), Path("/custom/mngr"))

    assert resolved == Path("/custom/mngr/my_logs")


def test_setup_logging_creates_log_dir(temp_mngr_ctx: MngrContext) -> None:
    """setup_logging should create the log directory if it doesn't exist."""
    log_dir = temp_mngr_ctx.config.default_host_dir / temp_mngr_ctx.config.logging.log_dir
    assert not log_dir.exists()

    logging_config = LoggingConfig(console_level=LogLevel.INFO)

    setup_logging(logging_config, default_host_dir=temp_mngr_ctx.config.default_host_dir, command="test")

    # The source subdirectory should be created
    source_dir = log_dir / "logs" / "mngr"
    assert source_dir.exists()
    assert source_dir.is_dir()


def test_setup_logging_creates_events_jsonl_file(temp_mngr_ctx: MngrContext) -> None:
    """setup_logging should create an events.jsonl file in the source directory."""
    log_dir = temp_mngr_ctx.config.default_host_dir / temp_mngr_ctx.config.logging.log_dir
    logging_config = LoggingConfig(console_level=LogLevel.INFO)

    setup_logging(logging_config, default_host_dir=temp_mngr_ctx.config.default_host_dir, command="test")

    # Log a message to trigger file creation
    logger.info("test log message")

    events_file = log_dir / "logs" / "mngr" / "events.jsonl"
    assert events_file.exists()


def test_setup_logging_writes_flat_jsonl_with_envelope_and_loguru_fields(temp_mngr_ctx: MngrContext) -> None:
    """setup_logging should write flat JSON log lines with envelope fields and loguru metadata."""
    log_dir = temp_mngr_ctx.config.default_host_dir / temp_mngr_ctx.config.logging.log_dir
    logging_config = LoggingConfig(console_level=LogLevel.INFO)

    setup_logging(logging_config, default_host_dir=temp_mngr_ctx.config.default_host_dir, command="list")

    logger.info("Listed 3 agents")

    events_file = log_dir / "logs" / "mngr" / "events.jsonl"
    content = events_file.read_text().strip()
    assert content, "events.jsonl should not be empty"

    # Parse the last line as flat JSON
    last_line = content.split("\n")[-1]
    parsed = json.loads(last_line)

    # Verify envelope fields at top level (same as bash logs)
    assert "timestamp" in parsed
    assert parsed["type"] == "mngr"
    assert parsed["event_id"].startswith("evt-")
    assert parsed["source"] == "logs/mngr"
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


def test_setup_logging_uses_custom_log_file_path(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    """setup_logging should create log file at custom path when log_file_path is provided."""
    custom_log_path = tmp_path / "custom_log.jsonl"

    logging_config = LoggingConfig(
        console_level=LogLevel.INFO,
        log_file_path=custom_log_path,
    )

    setup_logging(logging_config, default_host_dir=temp_mngr_ctx.config.default_host_dir, command="test")

    # Log a message to create the file
    logger.info("custom path test")
    assert custom_log_path.exists()


def test_setup_logging_creates_parent_dirs_for_custom_log_path(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    """setup_logging should create parent directories for custom log file path."""
    custom_log_path = tmp_path / "nested" / "dirs" / "custom_log.jsonl"

    assert not custom_log_path.parent.exists()

    logging_config = LoggingConfig(
        console_level=LogLevel.INFO,
        log_file_path=custom_log_path,
    )

    setup_logging(logging_config, default_host_dir=temp_mngr_ctx.config.default_host_dir, command="test")

    assert custom_log_path.parent.exists()


def test_setup_logging_expands_user_in_custom_log_path(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
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

    setup_logging(logging_config, default_host_dir=temp_mngr_ctx.config.default_host_dir, command="test")

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


def test_remove_console_handlers_clears_handler_id(temp_mngr_ctx: MngrContext) -> None:
    """remove_console_handlers should clear _console_handler_id."""
    logging_config = LoggingConfig(console_level=LogLevel.INFO)

    # Setup logging to populate console handler ID
    setup_logging(logging_config, default_host_dir=temp_mngr_ctx.config.default_host_dir, command="test")
    assert mngr_logging_module._console_handler_id is not None

    # Remove handlers
    remove_console_handlers()

    # Handler ID should be None
    assert mngr_logging_module._console_handler_id is None


def test_remove_console_handlers_is_idempotent(temp_mngr_ctx: MngrContext) -> None:
    """Calling remove_console_handlers twice should not error."""
    logging_config = LoggingConfig(console_level=LogLevel.INFO)

    setup_logging(logging_config, default_host_dir=temp_mngr_ctx.config.default_host_dir, command="test")
    remove_console_handlers()

    # Second call should not raise an error
    remove_console_handlers()


def test_remove_console_handlers_when_no_handlers_exist() -> None:
    """remove_console_handlers should not error when no handlers exist."""
    mngr_logging_module._console_handler_id = None

    # Should not raise an error
    remove_console_handlers()
    assert mngr_logging_module._console_handler_id is None


# =============================================================================
# Regression tests for dynamic sinks
# =============================================================================


def test_setup_logging_writes_to_current_stderr_after_stream_replacement(
    temp_mngr_ctx: MngrContext,
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

    setup_logging(logging_config, default_host_dir=temp_mngr_ctx.config.default_host_dir, command="test")

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


# =============================================================================
# Tests for _ParamikoToLoguruHandler
# =============================================================================


def _emit_paramiko_record(handler: _ParamikoToLoguruHandler, message: str, level: int) -> None:
    """Create and emit a logging record through the paramiko handler."""
    record = logging.LogRecord(
        name="paramiko.transport",
        level=level,
        pathname="transport.py",
        lineno=2288,
        msg=message,
        args=(),
        exc_info=None,
    )
    handler.emit(record)


@pytest.mark.parametrize(
    "message, input_level, expected_substring, sink_level",
    [
        pytest.param(
            "Exception (client): Error reading SSH protocol banner",
            logging.ERROR,
            "Exception (client)",
            "DEBUG",
            id="ssh_banner_error_to_debug",
        ),
        pytest.param(
            "Socket exception: Connection refused (111)",
            logging.ERROR,
            "Socket exception",
            "DEBUG",
            id="socket_exception_to_debug",
        ),
        pytest.param(
            "EOF in transport thread",
            logging.ERROR,
            "EOF in transport",
            "DEBUG",
            id="eof_to_debug",
        ),
        pytest.param(
            "Some paramiko warning",
            logging.WARNING,
            "warning",
            "DEBUG",
            id="warning_level_to_debug",
        ),
        pytest.param(
            "starting thread (client mode)",
            logging.DEBUG,
            "starting thread",
            "TRACE",
            id="debug_level_to_trace",
        ),
    ],
)
def test_paramiko_handler_routes_expected_messages(
    message: str,
    input_level: int,
    expected_substring: str,
    sink_level: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Enable paramiko logging so DEBUG-level messages are routed to TRACE
    monkeypatch.setenv("MNGR_ENABLE_PARAMIKO_LOGGING", "1")
    handler = _ParamikoToLoguruHandler()
    messages: list[str] = []
    handler_id = logger.add(lambda msg: messages.append(msg), level=sink_level)
    try:
        _emit_paramiko_record(handler, message, input_level)
        assert any("[paramiko]" in m and expected_substring in m for m in messages)
    finally:
        logger.remove(handler_id)


def test_paramiko_handler_routes_joined_traceback_body_to_debug() -> None:
    """A joined traceback body (from tb_strings()) should be routed to debug.

    Paramiko logs the header ("Exception (client): ...") and the traceback body
    (from util.tb_strings()) as separate _log calls. The body starts with
    "Traceback (most recent call last):" and should be matched by the regex.
    """
    handler = _ParamikoToLoguruHandler()
    messages: list[str] = []
    handler_id = logger.add(lambda msg: messages.append(msg), level="DEBUG")
    try:
        joined_traceback_body = (
            "Traceback (most recent call last):\n"
            '  File "/path/to/transport.py", line 42, in run\n'
            "    self._check_banner()\n"
            "paramiko.ssh_exception.SSHException: banner error"
        )
        _emit_paramiko_record(handler, joined_traceback_body, logging.ERROR)
        paramiko_messages = [m for m in messages if "[paramiko]" in m]
        assert len(paramiko_messages) == 1
        assert "Traceback" in paramiko_messages[0]
    finally:
        logger.remove(handler_id)


def test_paramiko_handler_routes_unknown_error_to_warning() -> None:
    """Unexpected paramiko errors should remain visible at warning level."""
    handler = _ParamikoToLoguruHandler()
    messages: list[str] = []
    handler_id = logger.add(lambda msg: messages.append(msg), level="WARNING")
    try:
        _emit_paramiko_record(handler, "Something completely unexpected happened", logging.ERROR)
        assert any("[paramiko]" in m and "unexpected" in m for m in messages)
    finally:
        logger.remove(handler_id)


def test_paramiko_handler_expected_errors_not_routed_to_warning() -> None:
    """Expected connection errors should NOT appear at warning level."""
    handler = _ParamikoToLoguruHandler()
    messages: list[str] = []
    handler_id = logger.add(lambda msg: messages.append(msg), level="WARNING")
    try:
        # Header (separate _log call)
        _emit_paramiko_record(handler, "Exception (client): Error reading SSH protocol banner", logging.ERROR)
        # Traceback body (joined by our patch, starts with "Traceback")
        _emit_paramiko_record(
            handler,
            'Traceback (most recent call last):\n  File "transport.py"\nparamiko.SSHException: banner',
            logging.ERROR,
        )
        assert not any("[paramiko]" in m for m in messages)
    finally:
        logger.remove(handler_id)


def test_paramiko_transport_log_patch_joins_list_messages() -> None:
    """The _log patch should join traceback list into a single log record.

    Paramiko's Transport._log calls self.logger.log(level, m) for each line
    in a list. Our patch joins the list into one message so the handler sees
    a single record starting with "Traceback (most recent call last):".
    """
    suppress_warnings()

    messages: list[str] = []
    handler_id = logger.add(lambda msg: messages.append(msg), level="DEBUG")
    try:
        # This is what util.tb_strings() returns -- the traceback body only,
        # NOT the "Exception (client):" header (which is a separate _log call)
        traceback_lines = [
            "Traceback (most recent call last):",
            '  File "transport.py", line 2373, in _check_banner',
            "    raise SSHException(",
            "paramiko.ssh_exception.SSHException: Error reading SSH protocol banner",
        ]
        transport_self = types.SimpleNamespace(
            logger=logging.getLogger("paramiko.transport"),
        )
        # Call the patched function directly (it accepts self: Any)
        _patched_transport_log(transport_self, logging.ERROR, traceback_lines)

        # The patch should join the list into one record (not 4 separate ones)
        paramiko_messages = [m for m in messages if "[paramiko]" in m]
        assert len(paramiko_messages) == 1
        assert "Traceback" in paramiko_messages[0]
        assert "banner" in paramiko_messages[0]
    finally:
        logger.remove(handler_id)


# =============================================================================
# Tests for threading.excepthook
# =============================================================================


def _get_paramiko_traceback() -> types.TracebackType:
    try:
        cast(Any, paramiko.channel.Channel._send)(None, b"test", None)
    except (AttributeError, TypeError, OSError) as e:
        assert e.__traceback__ is not None
        return e.__traceback__
    raise AssertionError("should not reach here")


def _make_paramiko_socket_closed_args() -> threading.ExceptHookArgs:
    exc = OSError("Socket is closed")
    tb = _get_paramiko_traceback()
    return threading.ExceptHookArgs((type(exc), exc, tb, threading.current_thread()))


def _make_paramiko_unexpected_args() -> threading.ExceptHookArgs:
    exc = RuntimeError("something unexpected in paramiko")
    tb = _get_paramiko_traceback()
    return threading.ExceptHookArgs((type(exc), exc, tb, threading.current_thread()))


def _make_non_paramiko_args() -> threading.ExceptHookArgs:
    try:
        raise RuntimeError("not paramiko")
    except RuntimeError as e:
        return threading.ExceptHookArgs((type(e), e, e.__traceback__, threading.current_thread()))
    raise AssertionError("should not reach here")


def test_is_expected_paramiko_thread_exception_matches_socket_closed() -> None:
    """The specific paramiko "Socket is closed" OSError should be recognized as expected."""
    args = _make_paramiko_socket_closed_args()
    assert _is_expected_paramiko_thread_exception(args)


def test_is_expected_paramiko_thread_exception_rejects_other_paramiko_errors() -> None:
    """Other paramiko errors (not "Socket is closed") should not be considered expected."""
    args = _make_paramiko_unexpected_args()
    assert not _is_expected_paramiko_thread_exception(args)


def test_is_expected_paramiko_thread_exception_rejects_non_paramiko() -> None:
    args = _make_non_paramiko_args()
    assert not _is_expected_paramiko_thread_exception(args)


def test_threading_excepthook_routes_expected_paramiko_to_debug() -> None:
    """The known paramiko "Socket is closed" error should be logged at debug."""
    args = _make_paramiko_socket_closed_args()

    messages: list[str] = []
    handler_id = logger.add(lambda msg: messages.append(msg), level="DEBUG")
    try:
        _threading_excepthook(args)
        paramiko_messages = [m for m in messages if "[paramiko]" in m]
        assert len(paramiko_messages) == 1
        assert "Expected exception" in paramiko_messages[0]
    finally:
        logger.remove(handler_id)


@pytest.mark.parametrize(
    "make_args",
    [
        pytest.param(_make_paramiko_unexpected_args, id="unexpected-paramiko"),
        pytest.param(_make_non_paramiko_args, id="non-paramiko"),
    ],
)
def test_threading_excepthook_routes_non_expected_exceptions_to_error(
    make_args: Callable[[], threading.ExceptHookArgs],
) -> None:
    """Any thread exception that is not the known paramiko error should be logged at error."""
    args = make_args()

    messages: list[str] = []
    handler_id = logger.add(lambda msg: messages.append(msg), level="ERROR")
    try:
        _threading_excepthook(args)
        error_messages = [m for m in messages if "Unhandled exception in thread" in m]
        assert len(error_messages) == 1
    finally:
        logger.remove(handler_id)


def test_suppress_warnings_installs_threading_excepthook() -> None:
    """suppress_warnings should install our threading excepthook."""
    mngr_logging_module._IS_THREADING_EXCEPTHOOK_INSTALLED["installed"] = False
    original = threading.excepthook
    try:
        suppress_warnings()
        assert threading.excepthook is _threading_excepthook
    finally:
        threading.excepthook = original
        mngr_logging_module._IS_THREADING_EXCEPTHOOK_INSTALLED["installed"] = False
