import sys
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final

import loguru
from loguru import logger

from imbue.imbue_common.enums import UpperCaseStrEnum
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


# Map our enum to loguru level strings
_LEVEL_MAP = {
    ConsoleLogLevel.TRACE: "TRACE",
    ConsoleLogLevel.DEBUG: "DEBUG",
    ConsoleLogLevel.INFO: "INFO",
    ConsoleLogLevel.WARN: "WARNING",
    ConsoleLogLevel.ERROR: "ERROR",
}


# Private marker attribute set on an exception instance the first time it is reported through an
# error-level loguru call. Subsequent error-level logs of the *same* instance are dropped, so a
# single failure caught and re-logged at multiple stack frames is reported only once. The marker
# lives on the exception (rather than in a side registry) so its lifetime matches the exception's:
# it is freed when the exception is garbage collected, never retaining tracebacks/frames. A side
# registry is not viable here because built-in exceptions (ValueError, KeyError, ...) are not
# weak-referenceable, so their lifecycle cannot be tracked weakly. The name is deliberately specific
# to avoid colliding with real exception attributes.
_ALREADY_REPORTED_ATTR: Final[str] = "_minds_exception_already_reported_to_logger"

# Key set on a loguru record's ``extra`` mapping by the dedup patcher to flag that the record
# carries an exception that was already reported. Sinks drop such records via
# ``is_not_duplicate_exception``.
_DUPLICATE_EXCEPTION_EXTRA_KEY: Final[str] = "_is_duplicate_exception_report"

# Only error-level (and above) records participate in exception dedup. Logging an exception at a
# lower level (e.g. warning/debug) must never mark it as reported, otherwise a genuine later
# ``logger.error`` for the same instance would be silently suppressed. 40 is loguru's ERROR level.
_ERROR_LEVEL_NO: Final[int] = 40


def _dedup_exception_patcher(record: "loguru.Record") -> None:
    """Global loguru patcher that marks and repeat-detects exceptions reported at error level.

    Runs once per log record (before any sink). For error-level records that carry an exception
    instance, it sets a private marker attribute on that instance the first time it is seen and, on
    any subsequent error-level record carrying the same already-marked instance, flags the record so
    sinks can drop it (see ``is_not_duplicate_exception``).

    This makes ``logger.opt(exception=e).error(...)`` deduplicate automatically: a single failure
    caught and re-logged at multiple stack frames is reported only once, with no need for callers to
    invoke a dedicated helper.
    """
    exception = record["exception"]
    if exception is None:
        return
    if record["level"].no < _ERROR_LEVEL_NO:
        return
    exc_value = exception.value
    if exc_value is None:
        return
    # ``getattr``/``setattr`` are required here: we attach dedup state to arbitrary exception
    # instances we do not own, for which no typed attribute exists.
    if getattr(exc_value, _ALREADY_REPORTED_ATTR, False):
        record["extra"][_DUPLICATE_EXCEPTION_EXTRA_KEY] = True
        return
    try:
        setattr(exc_value, _ALREADY_REPORTED_ATTR, True)
    except (AttributeError, TypeError):
        # A few exotic exception types (e.g. those using __slots__ without a matching slot) do not
        # accept arbitrary attributes; such instances simply opt out of dedup.
        pass


def is_not_duplicate_exception(record: "loguru.Record") -> bool:
    """Loguru sink filter that drops records flagged as duplicate exception reports.

    Pairs with ``_dedup_exception_patcher``: the patcher decides, this predicate enforces. Records
    without the flag (the common case) always pass.
    """
    return not record["extra"].get(_DUPLICATE_EXCEPTION_EXTRA_KEY, False)


def install_exception_dedup_patcher() -> None:
    """Install the global loguru patcher that powers automatic exception-report dedup.

    Idempotent: configuring the same patcher repeatedly is harmless, and ``logger.configure`` only
    replaces the patcher (it leaves existing sinks untouched). Call this before adding sinks that
    use ``is_not_duplicate_exception`` as a filter.
    """
    logger.configure(patcher=_dedup_exception_patcher)


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

    # Automatic exception-report dedup: a failure caught and re-logged at multiple stack frames
    # (the same exception instance) is reported only once. Installed before the sinks below so they
    # can filter on the dedup flag.
    install_exception_dedup_patcher()

    # Stderr console handler -- always human-readable, controlled by -v/-q
    if console_level != ConsoleLogLevel.NONE:
        logger.add(
            _dynamic_stderr_sink,
            level=_LEVEL_MAP[console_level],
            format=_format_user_message,
            colorize=False,
            diagnose=False,
            filter=is_not_duplicate_exception,
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
            filter=is_not_duplicate_exception,
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
