import sys
from enum import auto
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import make_jsonl_file_sink

# ANSI color codes that work well on both light and dark backgrounds.
# Uses 256-color palette codes (matching mngr's approach).
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


class _LevelStyle(FrozenModel):
    """How a console log level maps onto loguru and how its lines are rendered."""

    loguru_name: str = Field(
        description='The loguru level name this console level corresponds to (loguru spells WARN as "WARNING").'
    )
    color: str | None = Field(description="ANSI color wrapping the whole line, or None to render it plain (INFO).")
    prefix: str = Field(description='Text shown before the message (e.g. "WARNING: "); empty for no prefix.')

    def loguru_format(self) -> str:
        """The loguru format template for a line at this level, with color and prefix applied."""
        if self.color is None:
            return f"{self.prefix}{{message}}\n"
        return f"{self.color}{self.prefix}{{message}}{_RESET_COLOR}\n"


# Single source of truth for every console-output level: its loguru name (used to
# set the sink threshold) plus how its lines are rendered. ``ConsoleLogLevel.NONE``
# is intentionally absent: it means "add no console sink at all", so it has no
# loguru level and is filtered out by ``setup_logging`` before any lookup here.
_LEVEL_STYLES: dict[ConsoleLogLevel, _LevelStyle] = {
    ConsoleLogLevel.TRACE: _LevelStyle(loguru_name="TRACE", color=_TRACE_COLOR, prefix=""),
    ConsoleLogLevel.DEBUG: _LevelStyle(loguru_name="DEBUG", color=_DEBUG_COLOR, prefix=""),
    ConsoleLogLevel.INFO: _LevelStyle(loguru_name="INFO", color=None, prefix=""),
    ConsoleLogLevel.WARN: _LevelStyle(loguru_name="WARNING", color=_WARNING_COLOR, prefix="WARNING: "),
    ConsoleLogLevel.ERROR: _LevelStyle(loguru_name="ERROR", color=_ERROR_COLOR, prefix="ERROR: "),
}

# Reverse lookup for the stderr sink, which receives records tagged with loguru
# level *names* rather than our enum. Levels absent here -- loguru's own SUCCESS /
# CRITICAL, which minds does not emit -- render plain via the ``None`` default.
_STYLE_BY_LOGURU_NAME: dict[str, _LevelStyle] = {style.loguru_name: style for style in _LEVEL_STYLES.values()}


def _dynamic_stderr_sink(message: Any) -> None:
    """Loguru sink that always writes to the current sys.stderr.

    Using a callable sink (instead of passing sys.stderr directly) ensures
    that log output goes to the correct stream even when sys.stderr is
    replaced (e.g. by pytest's capture mechanism).
    """
    sys.stderr.write(str(message))
    sys.stderr.flush()


def _format_user_message(record: Any) -> str:
    """Format user-facing log lines: colored, with a WARNING:/ERROR: prefix where applicable."""
    style = _STYLE_BY_LOGURU_NAME.get(record["level"].name)
    # Loguru levels minds does not emit (its own SUCCESS/CRITICAL) are absent from
    # the table and render plain. If minds ever logs at CRITICAL, revisit whether
    # it should get the error color.
    if style is None:
        return "{message}\n"
    return style.loguru_format()


def setup_logging(
    console_level: ConsoleLogLevel,
    command: str = "unknown",
    log_file: Path | None = None,
) -> None:
    """Configure loguru logging for minds CLI.

    Sets up stderr logging with colored user-friendly formatting, and
    optionally a JSONL file sink for persistent log storage. Follows the
    same logging conventions as mngr: all logger.* output goes to stderr,
    stdout is reserved for command output (controlled separately via
    --format).

    The ``command`` parameter is included in JSONL file events.
    """
    logger.remove()

    # Stderr console handler -- always human-readable, controlled by -v/-q
    if console_level != ConsoleLogLevel.NONE:
        logger.add(
            _dynamic_stderr_sink,
            level=_LEVEL_STYLES[console_level].loguru_name,
            format=_format_user_message,
            colorize=False,
            diagnose=False,
        )

    # Optional JSONL file sink for persistent logging
    if log_file is not None:
        log_file = log_file.expanduser()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        jsonl_sink = make_jsonl_file_sink(
            file_path=str(log_file),
            event_type="minds",
            event_source="logs/minds",
            command=command,
            max_size_bytes=10 * 1024 * 1024,
        )
        logger.add(
            jsonl_sink,
            level="DEBUG",
            format="{message}",
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
