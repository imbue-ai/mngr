import sys
from enum import auto
from typing import Any

from loguru import logger

from imbue.imbue_common.enums import UpperCaseStrEnum

# ANSI color codes that work well on both light and dark backgrounds.
# Uses 256-color palette codes (matching mng's approach).
_WARNING_COLOR = "\x1b[1;38;5;178m"
_ERROR_COLOR = "\x1b[1;38;5;196m"
_DEBUG_COLOR = "\x1b[38;5;33m"
_TRACE_COLOR = "\x1b[38;5;99m"
_RESET_COLOR = "\x1b[0m"


class ConsoleLogLevel(UpperCaseStrEnum):
    """Log verbosity level for console output."""

    TRACE = auto()
    DEBUG = auto()
    INFO = auto()
    WARN = auto()
    ERROR = auto()
    NONE = auto()


def _dynamic_stderr_sink(message: Any) -> None:
    """Loguru sink that always writes to the current sys.stderr.

    Using a callable sink (instead of passing sys.stderr directly) ensures
    that log output goes to the correct stream even when sys.stderr is
    replaced (e.g. by pytest's capture mechanism).
    """
    sys.stderr.write(str(message))
    sys.stderr.flush()


def _format_user_message(record: Any) -> str:
    """Format user-facing log messages with colored prefixes for warnings and errors."""
    level_name = record["level"].name
    if level_name == "WARNING":
        return f"{_WARNING_COLOR}WARNING: {{message}}{_RESET_COLOR}\n"
    if level_name == "ERROR":
        return f"{_ERROR_COLOR}ERROR: {{message}}{_RESET_COLOR}\n"
    if level_name == "DEBUG":
        return f"{_DEBUG_COLOR}{{message}}{_RESET_COLOR}\n"
    if level_name == "TRACE":
        return f"{_TRACE_COLOR}{{message}}{_RESET_COLOR}\n"
    return "{message}\n"


def setup_logging(console_level: ConsoleLogLevel) -> None:
    """Configure loguru logging for changelings CLI.

    Sets up a single console handler on stderr with user-friendly formatting.
    No file logging is configured (unlike mng, changelings does not need
    persistent log files).
    """
    logger.remove()

    if console_level == ConsoleLogLevel.NONE:
        return

    # Map our enum to loguru level strings
    level_map = {
        ConsoleLogLevel.TRACE: "TRACE",
        ConsoleLogLevel.DEBUG: "DEBUG",
        ConsoleLogLevel.INFO: "INFO",
        ConsoleLogLevel.WARN: "WARNING",
        ConsoleLogLevel.ERROR: "ERROR",
    }

    logger.add(
        _dynamic_stderr_sink,
        level=level_map[console_level],
        format=_format_user_message,
        colorize=False,
        diagnose=False,
    )


def console_level_from_verbose_and_quiet(verbose: int, quiet: bool) -> ConsoleLogLevel:
    """Determine the console log level from -v/-q flags.

    Default (no flags): INFO
    -v: DEBUG
    -vv: TRACE
    -q: NONE (suppresses all output)
    """
    if quiet:
        return ConsoleLogLevel.NONE
    if verbose >= 2:
        return ConsoleLogLevel.TRACE
    if verbose == 1:
        return ConsoleLogLevel.DEBUG
    return ConsoleLogLevel.INFO
