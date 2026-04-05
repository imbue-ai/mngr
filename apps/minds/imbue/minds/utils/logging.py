import json
import os
import sys
from enum import auto
from typing import Any
from typing import assert_never

from loguru import logger

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id

# ANSI color codes that work well on both light and dark backgrounds.
# Uses 256-color palette codes (matching mngr's approach).
_WARNING_COLOR = "\x1b[1;38;5;178m"
_ERROR_COLOR = "\x1b[1;38;5;196m"
_DEBUG_COLOR = "\x1b[38;5;33m"
_TRACE_COLOR = "\x1b[38;5;99m"
_RESET_COLOR = "\x1b[0m"

# Module-level state for the JSONL sink. Using a mutable dict so
# setup_logging() can update it without a ``global`` statement.
_jsonl_state: dict[str, str] = {"command": "unknown"}


class ConsoleLogLevel(UpperCaseStrEnum):
    """Log verbosity level for console output."""

    TRACE = auto()
    DEBUG = auto()
    INFO = auto()
    WARN = auto()
    ERROR = auto()
    NONE = auto()


class LogFormat(UpperCaseStrEnum):
    """Output format for log messages."""

    TEXT = auto()
    JSONL = auto()


def _dynamic_stderr_sink(message: Any) -> None:
    """Loguru sink that always writes to the current sys.stderr.

    Using a callable sink (instead of passing sys.stderr directly) ensures
    that log output goes to the correct stream even when sys.stderr is
    replaced (e.g. by pytest's capture mechanism).
    """
    sys.stderr.write(str(message))
    sys.stderr.flush()


def _build_jsonl_event(record: Any) -> dict[str, Any]:
    """Build a flat JSONL event dict from a loguru record for Electron parsing."""
    event: dict[str, Any] = {
        "timestamp": format_nanosecond_iso_timestamp(record["time"]),
        "type": "minds",
        "event_id": generate_log_event_id(),
        "source": "minds",
        "level": record["level"].name,
        "message": record["message"],
        "pid": os.getpid(),
        "command": _jsonl_state["command"],
    }

    extra = dict(record["extra"])
    if extra:
        event["extra"] = extra

    return event


def _jsonl_stderr_sink(message: Any) -> None:
    """Loguru sink that writes JSONL-formatted log lines to stderr."""
    record = message.record
    event = _build_jsonl_event(record)
    json_line = json.dumps(event, separators=(",", ":"), default=str) + "\n"
    sys.stderr.write(json_line)
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


def setup_logging(
    console_level: ConsoleLogLevel,
    log_format: LogFormat = LogFormat.TEXT,
    command: str = "unknown",
) -> None:
    """Configure loguru logging for minds CLI.

    When log_format is TEXT, sets up a human-readable colored console handler.
    When log_format is JSONL, emits structured JSONL lines to stderr for
    machine parsing (used by the Electron desktop app).

    The ``command`` parameter is included in every JSONL event so consumers
    can distinguish which CLI subcommand produced the log line.
    """
    _jsonl_state["command"] = command

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

    match log_format:
        case LogFormat.TEXT:
            logger.add(
                _dynamic_stderr_sink,
                level=level_map[console_level],
                format=_format_user_message,
                colorize=False,
                diagnose=False,
            )
        case LogFormat.JSONL:
            logger.add(
                _jsonl_stderr_sink,
                level=level_map[console_level],
                format="{message}",
                colorize=False,
                diagnose=False,
            )
        case _ as unreachable:
            assert_never(unreachable)


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
