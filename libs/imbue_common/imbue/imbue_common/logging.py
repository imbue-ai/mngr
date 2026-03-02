import functools
import inspect
import json
import os
import sys
import time
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Final
from typing import ParamSpec
from typing import TypeVar
from uuid import uuid4

from loguru import logger

from imbue.imbue_common.pure import pure


def setup_logging(level: str = "INFO") -> None:
    """Configure loguru logging with the specified level."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        format="<level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    )


P = ParamSpec("P")
R = TypeVar("R")


_MAX_LOG_VALUE_REPR_LENGTH: Final[int] = 200


@pure
def _format_arg_value(value: Any) -> str:
    """Format an argument value for logging, truncating if too long."""
    str_value = repr(value)
    if len(str_value) > _MAX_LOG_VALUE_REPR_LENGTH:
        return str_value[: _MAX_LOG_VALUE_REPR_LENGTH - 3] + "..."
    return str_value


def log_call(func: Callable[P, R]) -> Callable[P, R]:
    """Decorator that logs function calls with inputs and outputs at debug level.

    Logs the function name and binds arguments as structured logging fields.
    Useful for API entry points to trace execution.
    """
    # Get the function name once at decoration time
    func_name = getattr(func, "__name__", repr(func))

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        # Get the function signature to map positional args to names
        sig = inspect.signature(func)
        bound_args = sig.bind(*args, **kwargs)
        bound_args.apply_defaults()

        # Build structured logging fields from arguments
        log_fields = {name: _format_arg_value(value) for name, value in bound_args.arguments.items()}
        logger.debug("Calling {}", func_name, **log_fields)

        start_time = time.monotonic()

        result = func(*args, **kwargs)

        elapsed = time.monotonic() - start_time
        done_message = f"Calling {func_name} [done in {elapsed:.5f} sec]"
        logger.trace(done_message, result=_format_arg_value(result))

        return result

    return wrapper


@contextmanager
def log_span(message: str, *args: Any, **context: Any) -> Iterator[None]:
    """Context manager that logs a debug message on entry and a trace message with timing on exit.

    On entry, emits logger.debug(message, *args).
    On exit, emits logger.trace(message + " [done in X.XXXXX sec]", *args, elapsed).

    Keyword arguments are passed to logger.contextualize so that all log messages
    within the span include the extra context fields.
    """
    with logger.contextualize(**context):
        logger.debug(message, *args)
        start_time = time.monotonic()
        try:
            yield
        except BaseException:
            elapsed = time.monotonic() - start_time
            failed_message = message + " [failed after {:.5f} sec]"
            logger.trace(failed_message, *args, elapsed)
            raise
        else:
            elapsed = time.monotonic() - start_time
            done_message = message + " [done in {:.5f} sec]"
            logger.trace(done_message, *args, elapsed)


@contextmanager
def trace_span(message: str, *args: Any, _is_trace_span_enabled: bool = True, **context: Any) -> Iterator[None]:
    """Context manager that logs a trace message on entry and a trace message with timing on exit.

    On entry, emits logger.trace(message, *args).
    On exit, emits logger.trace(message + " [done in X.XXXXX sec]", *args, elapsed).

    Keyword arguments are passed to logger.contextualize so that all log messages
    within the span include the extra context fields.
    """
    if not _is_trace_span_enabled:
        yield
    else:
        with logger.contextualize(**context):
            logger.trace(message, *args)
            start_time = time.monotonic()
            try:
                yield
            except BaseException:
                elapsed = time.monotonic() - start_time
                failed_message = message + " [failed after {:.5f} sec]"
                logger.trace(failed_message, *args, elapsed)
                raise
            else:
                elapsed = time.monotonic() - start_time
                done_message = message + " [done in {:.5f} sec]"
                logger.trace(done_message, *args, elapsed)


# -- Flat JSONL formatting for loguru file sinks --
#
# Produces a single flat JSON object per log line that merges the event
# envelope fields with all standard loguru fields.  The field names are
# chosen so that the envelope fields (timestamp, type, event_id, source,
# level, message, pid) have the same names and positions as in the bash
# logs emitted by mng_log.sh.  Python logs simply have additional fields
# (function, line, module, extra, exception, etc.).


@pure
def format_nanosecond_iso_timestamp(dt: datetime) -> str:
    """Format a datetime as ISO 8601 with nanosecond precision in UTC.

    Converts to UTC first so the trailing 'Z' is always correct, even when
    loguru provides a local-timezone datetime.
    """
    utc_dt = dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc_dt.microsecond * 1000:09d}Z"


def generate_log_event_id() -> str:
    """Generate a unique event ID using a random UUID4 hex with 'evt-' prefix."""
    return f"evt-{uuid4().hex}"


def make_jsonl_log_formatter(
    event_type: str,
    event_source: str,
    command: str | None,
) -> Callable[..., str]:
    """Create a loguru format function that produces flat JSONL with event envelope fields.

    The returned callable builds a single flat JSON object from the loguru
    record, combining the event envelope fields (timestamp, type, event_id,
    source, level, message, pid, command) with flattened loguru metadata
    (function, line, module, file, elapsed, exception, process, thread, extra).

    Braces in the returned string are doubled so loguru's format_map does not
    interpret them as placeholders.
    """
    bound_type = event_type
    bound_source = event_source
    bound_command = command

    def formatter(record: Any) -> str:
        # Build the flat dict with envelope fields first, then loguru fields
        event: dict[str, Any] = {
            "timestamp": format_nanosecond_iso_timestamp(record["time"]),
            "type": bound_type,
            "event_id": generate_log_event_id(),
            "source": bound_source,
            "level": record["level"].name,
            "message": record["message"],
            "pid": os.getpid(),
        }
        if bound_command is not None:
            event["command"] = bound_command

        # Flattened loguru metadata
        event["function"] = record["function"]
        event["line"] = record["line"]
        event["module"] = record["module"]
        event["logger_name"] = record["name"]
        event["file_name"] = record["file"].name
        event["file_path"] = record["file"].path
        event["elapsed_seconds"] = record["elapsed"].total_seconds()

        # Exception info (None when no exception)
        exc = record["exception"]
        if exc is not None:
            event["exception"] = {
                "type": exc.type.__name__ if exc.type else None,
                "value": str(exc.value) if exc.value else None,
                "traceback": bool(exc.traceback),
            }
        else:
            event["exception"] = None

        # Process and thread
        event["process_name"] = record["process"].name
        event["thread_name"] = record["thread"].name
        event["thread_id"] = record["thread"].id

        # Extra context (from logger.contextualize or logger.bind)
        extra = dict(record["extra"])
        if extra:
            event["extra"] = extra

        json_line = json.dumps(event, separators=(",", ":"), default=str)
        # Escape braces so loguru's format_map does not interpret them
        return json_line.replace("{", "{{").replace("}", "}}") + "\n"

    return formatter
