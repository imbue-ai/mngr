import io
import logging
import re
import sys
from collections import deque
from pathlib import Path
from typing import Any
from typing import Final
from typing import TextIO
from typing import cast

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import make_jsonl_file_sink
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.mng.primitives import LogLevel

# Default event type and source for mng CLI logs
_DEFAULT_EVENT_TYPE: Final[str] = "mng"
_DEFAULT_EVENT_SOURCE: Final[str] = "logs/mng"


class LoggingConfig(FrozenModel):
    """Logging configuration for mng."""

    file_level: LogLevel = Field(
        default=LogLevel.DEBUG,
        description="Log level for file logging",
    )
    log_dir: Path = Field(
        default=Path("events"),
        description="Directory for event files (relative to data root if relative)",
    )
    max_log_size_mb: int = Field(
        default=10,
        description="Maximum size of each log file in MB",
    )
    console_level: LogLevel = Field(
        default=LogLevel.BUILD,
        description="Log level for console output",
    )
    log_file_path: Path | None = Field(
        default=None,
        description="Custom log file path (None for default)",
    )
    is_logging_commands: bool = Field(
        default=True,
        description="Log what commands were executed",
    )
    is_logging_command_output: bool = Field(
        default=False,
        description="Log stdout/stderr from executed commands",
    )
    is_logging_env_vars: bool = Field(
        default=False,
        description="Log environment variables (security risk)",
    )
    event_type: NonEmptyStr = Field(
        default=NonEmptyStr(_DEFAULT_EVENT_TYPE),
        description="Event type for JSONL log events (e.g. 'mng', 'minds')",
    )
    event_source: NonEmptyStr = Field(
        default=NonEmptyStr(_DEFAULT_EVENT_SOURCE),
        description="Event source for JSONL log events, matching events/<source>/",
    )

    def merge_with(self, override: "LoggingConfig") -> "LoggingConfig":
        """Merge this config with an override config.

        Important note: despite the type signatures, any of these fields may be None in the override--this means that they were NOT set in the toml (and thus should be ignored)

        Scalar fields: override wins if not None
        """
        return LoggingConfig(
            file_level=override.file_level if override.file_level is not None else self.file_level,
            log_dir=override.log_dir if override.log_dir is not None else self.log_dir,
            max_log_size_mb=override.max_log_size_mb if override.max_log_size_mb is not None else self.max_log_size_mb,
            console_level=override.console_level if override.console_level is not None else self.console_level,
            log_file_path=override.log_file_path if override.log_file_path is not None else self.log_file_path,
            is_logging_commands=override.is_logging_commands
            if override.is_logging_commands is not None
            else self.is_logging_commands,
            is_logging_command_output=override.is_logging_command_output
            if override.is_logging_command_output is not None
            else self.is_logging_command_output,
            is_logging_env_vars=override.is_logging_env_vars
            if override.is_logging_env_vars is not None
            else self.is_logging_env_vars,
            event_type=override.event_type if override.event_type is not None else self.event_type,
            event_source=override.event_source if override.event_source is not None else self.event_source,
        )


# ANSI color codes that work well on both light and dark backgrounds.
# Using 256-color palette codes with bold for better visibility.
# Falls back gracefully in terminals that don't support 256 colors.
# WARNING_COLOR: Bold gold/orange (256-color code 178)
# ERROR_COLOR: Bold red (256-color code 196)
# BUILD_COLOR: Medium gray (256-color code 245) - visible on both black and white backgrounds
# DEBUG_COLOR: Solid blue (256-color code 33)
# TRACE COLOR: Purple (256-color code 99)
WARNING_COLOR = "\x1b[1;38;5;178m"
ERROR_COLOR = "\x1b[1;38;5;196m"
BUILD_COLOR = "\x1b[38;5;245m"
DEBUG_COLOR = "\x1b[38;5;33m"
TRACE_COLOR = "\x1b[38;5;99m"
RESET_COLOR = "\x1b[0m"

# Custom loguru log level number for BUILD (between DEBUG=10 and INFO=20)
BUILD_LEVEL_NO: Final[int] = 15


def register_build_level() -> None:
    """Register the custom BUILD log level with loguru.

    This is called at module import time to ensure the BUILD level is always
    available when using logger.log("BUILD", ...). The function is idempotent
    and can be called multiple times safely.

    The BUILD level (severity 15) sits between DEBUG (10) and INFO (20),
    intended for image build output (Modal, Docker, etc).
    """
    try:
        logger.level("BUILD")
    except ValueError:
        # Level doesn't exist, create it
        logger.level("BUILD", no=BUILD_LEVEL_NO, color="<white>")


# Register BUILD level at module import time
register_build_level()

# Default buffer size for suppressed log messages
DEFAULT_BUFFER_SIZE: Final[int] = 500

# ANSI escape codes for screen control
CLEAR_SCREEN: Final[str] = "\x1b[2J\x1b[H"

# Module-level storage for the console handler ID (used by LoggingSuppressor)
_console_handler_id: int | None = None


# Map from our LogLevel enum to loguru level strings
LEVEL_MAP: Final[dict[LogLevel, str]] = {
    LogLevel.TRACE: "TRACE",
    LogLevel.DEBUG: "DEBUG",
    LogLevel.BUILD: "BUILD",
    LogLevel.INFO: "INFO",
    LogLevel.WARN: "WARNING",
    LogLevel.ERROR: "ERROR",
    LogLevel.NONE: "CRITICAL",
}


def _dynamic_stderr_sink(message: Any) -> None:
    """Loguru sink that always writes to the current sys.stderr."""
    sys.stderr.write(str(message))
    sys.stderr.flush()


def _format_user_message(record: Any) -> str:
    """Format user-facing log messages, adding colored prefixes for warnings and errors.

    The record parameter is a loguru Record TypedDict, but the type is only available
    in type stubs so we use Any here.
    """
    level_name = record["level"].name
    if level_name == "WARNING":
        return f"{WARNING_COLOR}WARNING: {{message}}{RESET_COLOR}\n"
    if level_name == "ERROR":
        return f"{ERROR_COLOR}ERROR: {{message}}{RESET_COLOR}\n"
    if level_name == "BUILD":
        return f"{BUILD_COLOR}{{message}}{RESET_COLOR}\n"
    if level_name == "DEBUG":
        return f"{DEBUG_COLOR}{{message}}{RESET_COLOR}\n"
    if level_name == "TRACE":
        return f"{TRACE_COLOR}{{message}}{RESET_COLOR}\n"
    return "{message}\n"


_PYINFRA_NOISE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\[@\w+\] Connected$"
    r"|^--> Running command on "
    r"|^--> Waiting for exit status"
    r"|^--> Command exit status: "
)
"""Patterns for pyinfra messages that are purely duplicative of mng's own logging.

These cover the standard command-execution flow (connect, run, wait, exit status)
that mng already wraps in its own log_span calls. Other pyinfra messages (errors,
warnings, upload retries, etc.) are kept so that unexpected situations are still
visible.
"""


class _PyinfraToLoguruHandler(logging.Handler):
    """Forward pyinfra log messages to loguru, suppressing duplicative noise.

    Pyinfra uses Python's standard logging module and outputs messages that mng
    already handles (e.g., connection status, command execution). This handler
    silently drops the standard operational messages (matched by _PYINFRA_NOISE_RE)
    and forwards everything else to loguru at TRACE level so that unexpected
    pyinfra output is still visible when debugging.
    """

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if _PYINFRA_NOISE_RE.search(msg):
            return
        logger.trace("[pyinfra] {}", msg)


def suppress_warnings() -> None:
    # Redirect all pyinfra log output to loguru at TRACE level. Pyinfra uses
    # Python's standard logging module and logs warnings during file upload
    # retries, errors during connection failures (e.g., authentication errors),
    # etc. Mng already handles these cases gracefully via exceptions, so the
    # pyinfra log output is noise at normal log levels. By redirecting to TRACE,
    # the messages are still available when debugging with --log-level trace.
    pyinfra_logger = logging.getLogger("pyinfra")
    pyinfra_logger.setLevel(logging.DEBUG)
    pyinfra_logger.handlers.clear()
    pyinfra_logger.addHandler(_PyinfraToLoguruHandler())
    pyinfra_logger.propagate = False


def setup_logging(
    config: LoggingConfig,
    default_host_dir: Path,
    command: str | None,
) -> None:
    """Configure logging based on the provided settings.

    Sets up:
    - stderr logging for user-facing messages (clean format, colored)
    - File logging in JSONL event envelope format to a single rotating events.jsonl
    """
    global _console_handler_id

    # Remove default handler
    logger.remove()

    # Suppress pyinfra warnings
    suppress_warnings()

    # BUILD level is registered at module import time via register_build_level()

    # Reset stored handler ID from previous setup (if any)
    _console_handler_id = None

    # Set up stderr logging for user-facing messages (clean format, with colored WARNING prefix).
    # All logger.* messages go to stderr; only explicit output (JSON, tables, etc.) goes to stdout.
    # We set colorize=False because we handle colors manually in _format_user_message.
    # Use callable sinks so the handler always writes to the current sys.stderr,
    # even if it gets replaced (e.g., by pytest's capture mechanism).
    if config.console_level != LogLevel.NONE:
        _console_handler_id = logger.add(
            _dynamic_stderr_sink,
            level=config.console_level,
            format=_format_user_message,
            colorize=False,
            diagnose=False,
        )

    # Set up file logging with flat JSONL format
    if config.log_file_path is not None:
        log_file = config.log_file_path.expanduser()
        log_file.parent.mkdir(parents=True, exist_ok=True)
    else:
        resolved_log_dir = _resolve_log_dir(config.log_dir, default_host_dir)
        # Write to events/<source>/events.jsonl
        log_source_dir = resolved_log_dir / config.event_source
        log_source_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_source_dir / "events.jsonl"

    # Use a callable sink (not a format function) to bypass loguru's colorizer,
    # which would otherwise choke on angle brackets in serialized extra data.
    loguru_file_level = LEVEL_MAP[config.file_level]
    jsonl_sink = make_jsonl_file_sink(
        file_path=str(log_file),
        event_type=config.event_type,
        event_source=config.event_source,
        command=command,
        max_size_bytes=config.max_log_size_mb * 1024 * 1024,
    )
    logger.add(
        jsonl_sink,
        level=loguru_file_level,
        format="{message}",
        colorize=False,
        diagnose=False,
    )


def _resolve_log_dir(log_dir: Path, default_host_dir: Path) -> Path:
    """Resolve the log directory path.

    If log_dir is relative, it's relative to default_host_dir.
    """
    if not log_dir.is_absolute():
        host_dir = default_host_dir.expanduser()
        log_dir = host_dir / log_dir

    return log_dir.expanduser()


class BufferedMessage(FrozenModel):
    """A buffered log message with its formatted output and destination."""

    formatted_message: str = Field(description="The formatted log message text")
    is_stderr: bool = Field(description="Whether this message should go to stderr")


class BufferingStreamWrapper(io.TextIOBase):
    """A stream wrapper that buffers all writes instead of passing them through.

    This is used to capture ALL writes to stdout/stderr, including those from
    Python's warnings module, third-party libraries, and any other code that
    writes directly to sys.stdout or sys.stderr.

    The wrapper maintains a reference to the original stream so it can be
    restored later, and stores all writes in a provided buffer.
    """

    def __init__(self, original_stream: TextIO, buffer: deque[BufferedMessage], is_stderr: bool) -> None:
        super().__init__()
        self._original_stream = original_stream
        self._buffer = buffer
        self._is_stderr = is_stderr
        self._encoding = getattr(original_stream, "encoding", "utf-8")
        self._errors = getattr(original_stream, "errors", "strict")

    @property
    def encoding(self) -> str:
        """Return the encoding of the original stream."""
        return self._encoding

    @property
    def errors(self) -> str:
        """Return the error handling mode of the original stream."""
        return self._errors

    def write(self, s: str) -> int:
        """Buffer the write instead of passing it to the original stream."""
        if s:
            self._buffer.append(BufferedMessage(formatted_message=s, is_stderr=self._is_stderr))
        return len(s)

    def flush(self) -> None:
        """No-op since we're buffering, not writing."""
        pass

    def isatty(self) -> bool:
        """Return whether the original stream is a TTY."""
        return self._original_stream.isatty()

    def fileno(self) -> int:
        """Return the file descriptor of the original stream.

        This is needed for code that checks the file descriptor (e.g., some
        terminal libraries).
        """
        return self._original_stream.fileno()

    @property
    def original_stream(self) -> TextIO:
        """Get the original stream for restoration."""
        return self._original_stream


class LoggingSuppressor:
    """Manages temporary suppression and buffering of console log output.

    When suppression is enabled, console log messages (stdout/stderr) are
    buffered instead of being written immediately. File logging is not affected.

    This class also redirects sys.stdout and sys.stderr to capture ALL writes,
    including those from Python's warnings module, third-party libraries, and
    any other code that writes directly to the streams.

    Use as a context manager or call enable/disable explicitly.
    """

    # Class-level state for the singleton suppressor
    _is_suppressed: bool = False
    _buffer: deque[BufferedMessage] = deque(maxlen=DEFAULT_BUFFER_SIZE)
    _suppressed_console_handler_id: int | None = None
    _console_level: LogLevel | None = None
    # Original streams for restoration
    _original_stdout: TextIO | None = None
    _original_stderr: TextIO | None = None

    @classmethod
    def is_suppressed(cls) -> bool:
        """Check if logging suppression is currently active."""
        return cls._is_suppressed

    @classmethod
    def enable(cls, console_level: LogLevel, buffer_size: int = DEFAULT_BUFFER_SIZE) -> None:
        """Enable logging suppression and start buffering console output.

        The buffer will keep the most recent buffer_size messages. File logging
        is not affected - only the console handler is suppressed.

        This also redirects sys.stdout and sys.stderr to capture ALL writes,
        including those from Python's warnings module and third-party libraries.
        """
        global _console_handler_id

        if cls._is_suppressed:
            return

        cls._console_level = console_level
        cls._buffer = deque(maxlen=buffer_size)
        cls._is_suppressed = True

        # Remove the console handler (preserving file logging)
        if _console_handler_id is not None:
            try:
                logger.remove(_console_handler_id)
            except ValueError:
                pass
            _console_handler_id = None

        # Redirect sys.stdout and sys.stderr to capture ALL writes
        # This captures Python warnings, third-party library output, etc.
        cls._original_stdout = sys.stdout
        cls._original_stderr = sys.stderr
        stdout_wrapper = BufferingStreamWrapper(cls._original_stdout, cls._buffer, is_stderr=False)
        stderr_wrapper = BufferingStreamWrapper(cls._original_stderr, cls._buffer, is_stderr=True)
        sys.stdout = cast(TextIO, stdout_wrapper)
        sys.stderr = cast(TextIO, stderr_wrapper)

        # Add a buffering handler that captures messages instead of writing to console
        if console_level != LogLevel.NONE:
            cls._suppressed_console_handler_id = logger.add(
                cls._buffered_console_sink,
                level=console_level,
                format=_format_user_message,
                colorize=False,
                diagnose=False,
            )

    @classmethod
    def _buffered_console_sink(cls, message: Any) -> None:
        """Sink function that buffers messages intended for the console (stderr)."""
        cls._buffer.append(BufferedMessage(formatted_message=str(message), is_stderr=True))

    @classmethod
    def disable_and_replay(cls, clear_screen: bool = True) -> None:
        """Disable suppression and replay buffered messages.

        If clear_screen is True, clears the terminal before replaying messages.
        Restores sys.stdout and sys.stderr to their original streams.
        """
        global _console_handler_id

        if not cls._is_suppressed:
            return

        cls._is_suppressed = False
        console_level = cls._console_level

        # Remove the buffering handler
        if cls._suppressed_console_handler_id is not None:
            logger.remove(cls._suppressed_console_handler_id)
            cls._suppressed_console_handler_id = None

        # Restore the original stdout/stderr streams BEFORE writing anything
        # This ensures our replayed messages go to the real terminal
        if cls._original_stdout is not None:
            sys.stdout = cls._original_stdout
            cls._original_stdout = None
        if cls._original_stderr is not None:
            sys.stderr = cls._original_stderr
            cls._original_stderr = None

        # Clear the screen if requested
        if clear_screen:
            sys.stdout.write(CLEAR_SCREEN)
            sys.stdout.flush()

        # Replay buffered messages to their original destinations
        for buffered_msg in cls._buffer:
            if buffered_msg.is_stderr:
                sys.stderr.write(buffered_msg.formatted_message)
            else:
                sys.stdout.write(buffered_msg.formatted_message)

        # Flush both streams
        sys.stdout.flush()
        sys.stderr.flush()

        # Clear the buffer
        cls._buffer.clear()

        # Re-add the normal console handler and store its ID
        if console_level is not None and console_level != LogLevel.NONE:
            _console_handler_id = logger.add(
                _dynamic_stderr_sink,
                level=console_level,
                format=_format_user_message,
                colorize=False,
                diagnose=False,
            )

        cls._console_level = None

    @classmethod
    def get_buffered_messages(cls) -> list[BufferedMessage]:
        """Get a copy of the current buffer contents."""
        return list(cls._buffer)


def remove_console_handlers() -> None:
    """Remove the console log handler.

    This is useful for daemon/background processes that detach from the terminal,
    where the console file descriptors may become invalid after the parent exits.
    File logging continues to work after calling this function.
    """
    global _console_handler_id
    if _console_handler_id is not None:
        try:
            logger.remove(_console_handler_id)
        except ValueError:
            # Handler already removed
            pass
        _console_handler_id = None
