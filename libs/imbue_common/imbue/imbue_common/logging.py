import fcntl
import functools
import inspect
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from typing import IO
from typing import ParamSpec
from typing import TypeVar
from uuid import uuid4

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
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
def info_span(message: str, *args: Any, **context: Any) -> Iterator[None]:
    """Context manager that logs an info message on entry and a trace message with timing on exit.

    On entry, emits logger.info(message, *args).
    On exit, emits logger.debug(message + " [done in X.XXXXX sec]", *args, elapsed).

    Keyword arguments are passed to logger.contextualize so that all log messages
    within the span include the extra context fields.
    """
    with logger.contextualize(**context):
        logger.info(message, *args)
        start_time = time.monotonic()
        is_success = False
        try:
            yield
            is_success = True
        finally:
            elapsed = time.monotonic() - start_time
            suffix = " [done in {:.5f} sec]" if is_success else " [failed after {:.5f} sec]"
            logger.debug(message + suffix, *args, elapsed)


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
# logs emitted by mngr_log.sh.  Python logs simply have additional fields
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


def _build_flat_log_dict(
    record: Any,
    event_type: str,
    event_source: str,
    command: str | None,
) -> dict[str, Any]:
    """Build a flat dict from a loguru record with envelope and metadata fields."""
    event: dict[str, Any] = {
        "timestamp": format_nanosecond_iso_timestamp(record["time"]),
        "type": event_type,
        "event_id": generate_log_event_id(),
        "source": event_source,
        "level": record["level"].name,
        "message": record["message"],
        "pid": os.getpid(),
    }
    if command is not None:
        event["command"] = command

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

    return event


# Filename of the default structured event log. Its rotated copies are named
# ``events.jsonl.<rotation_timestamp>`` and matched by ROTATED_JSONL_PATTERN.
_EVENTS_JSONL_BASENAME: Final[str] = "events.jsonl"


def rotated_file_pattern(base_name: str) -> re.Pattern[str]:
    """Return a regex matching ``<base_name>.<rotation_timestamp>`` rotated copies.

    The rotation timestamp is the all-digit string produced by
    :func:`generate_rotation_timestamp`, so the suffix is ``\\.(\\d+)``.
    """
    return re.compile(rf"^{re.escape(base_name)}\.(\d+)$")


ROTATED_JSONL_PATTERN: Final[re.Pattern[str]] = rotated_file_pattern(_EVENTS_JSONL_BASENAME)

_ROTATION_LOCK_FILENAME: Final[str] = ".rotation.lock"
_ROTATION_LOCK_WARNING_SECONDS: Final[float] = 3.0


@contextmanager
def rotation_lock(directory: Path) -> Iterator[None]:
    """Acquire an exclusive file lock for rotation operations in a directory.

    Uses fcntl.flock which is automatically released when the fd is closed,
    including if the process dies unexpectedly (the kernel closes the fd).
    Emits a warning if acquiring or holding the lock takes too long.
    """
    lock_path = directory / _ROTATION_LOCK_FILENAME
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        acquire_start = time.monotonic()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logger.warning("Waiting for rotation lock at {}", lock_path)
            fcntl.flock(fd, fcntl.LOCK_EX)
            acquire_elapsed = time.monotonic() - acquire_start
            if acquire_elapsed > _ROTATION_LOCK_WARNING_SECONDS:
                logger.warning("Rotation lock at {} took {:.1f}s to acquire", lock_path, acquire_elapsed)

        hold_start = time.monotonic()
        yield

        hold_elapsed = time.monotonic() - hold_start
        if hold_elapsed > _ROTATION_LOCK_WARNING_SECONDS:
            logger.warning("Rotation lock at {} was held for {:.1f}s", lock_path, hold_elapsed)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def generate_rotation_timestamp() -> str:
    """Generate a timestamp string for rotated file naming (YYYYMMDDHHMMSSffffff)."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond:06d}"


def cleanup_old_rotated_files(
    directory: Path,
    max_rotated_count: int,
    base_name: str = _EVENTS_JSONL_BASENAME,
) -> None:
    """Remove the oldest rotated copies of ``base_name``, keeping at most max_rotated_count.

    Rotated files are matched by name as ``<base_name>.<rotation_timestamp>``
    (an all-digit suffix). ``base_name`` defaults to ``events.jsonl`` so existing
    callers keep their behaviour; pass the real log filename when rotating other
    files so their rotated copies are pruned too (otherwise they would accumulate
    forever, since the ``events.jsonl`` pattern would never match them).
    """
    pattern = rotated_file_pattern(base_name)
    rotated_files = [child for child in directory.iterdir() if pattern.match(child.name)]
    rotated_files.sort(key=lambda p: p.name)
    files_to_remove = rotated_files[:-max_rotated_count] if max_rotated_count > 0 else rotated_files
    for old_file in files_to_remove:
        old_file.unlink(missing_ok=True)


def rotate_file_if_too_large(
    path: Path,
    max_size_bytes: int,
    max_rotated_count: int = 10,
) -> bool:
    """Rotate ``path`` out of the way if it already exceeds ``max_size_bytes``.

    Intended for append-only capture files whose file descriptor is handed
    directly to a detached subprocess (so they cannot be rotated mid-write):
    rotate them at (re)spawn time instead, which bounds growth across restarts.
    The current file is renamed to ``<name>.<rotation_timestamp>`` and at most
    ``max_rotated_count`` rotated copies are kept. Returns True when a rotation
    happened. Best-effort and concurrency-safe via :func:`rotation_lock`; a
    missing file is a no-op.
    """
    try:
        if path.stat().st_size < max_size_bytes:
            return False
    except OSError:
        return False
    with rotation_lock(path.parent):
        # Re-check under the lock: another spawner may have rotated already.
        try:
            if path.stat().st_size < max_size_bytes:
                return False
        except OSError:
            return False
        rotated = path.with_name(f"{path.name}.{generate_rotation_timestamp()}")
        path.rename(rotated)
        cleanup_old_rotated_files(path.parent, max_rotated_count, base_name=path.name)
    return True


def make_jsonl_file_sink(
    file_path: str,
    event_type: str,
    event_source: str,
    command: str | None,
    max_size_bytes: int,
    max_rotated_count: int = 10,
) -> Callable[..., None]:
    """Create a loguru sink function that writes flat JSONL to a rotating file.

    Bypasses loguru's colorizer entirely by using a callable sink instead of
    a format function. Handles file rotation when the file exceeds max_size_bytes.
    Keeps at most max_rotated_count rotated files, removing the oldest on rotation.
    Rotated files are named events.jsonl.<YYYYMMDDHHMMSSffffff>.
    Uses cooperative file locking to prevent races when multiple processes rotate.
    """
    bound_type = event_type
    bound_source = event_source
    bound_command = command
    bound_path = file_path
    bound_max_size = max_size_bytes
    bound_max_rotated = max_rotated_count

    # Mutable state for the file handle
    state: dict[str, Any] = {"file": None, "size": 0, "cleaned_up": False}

    def _ensure_file() -> Any:
        if state["file"] is None:
            Path(bound_path).parent.mkdir(parents=True, exist_ok=True)
            # Clean up old rotated files on first open
            if not state["cleaned_up"]:
                cleanup_old_rotated_files(Path(bound_path).parent, bound_max_rotated, base_name=Path(bound_path).name)
                state["cleaned_up"] = True
            state["file"] = open(bound_path, "a")
            try:
                state["size"] = Path(bound_path).stat().st_size
            except OSError:
                state["size"] = 0
        return state["file"]

    def _rotate_if_needed() -> None:
        if state["size"] >= bound_max_size:
            path = Path(bound_path)
            with rotation_lock(path.parent):
                # Re-check actual file size: another process may have already rotated
                try:
                    actual_size = path.stat().st_size
                except OSError:
                    actual_size = 0
                if actual_size < bound_max_size:
                    # Already rotated by another process -- reopen our handle
                    if state["file"] is not None:
                        state["file"].close()
                    state["file"] = open(bound_path, "a")
                    state["size"] = actual_size
                    return
                if state["file"] is not None:
                    state["file"].close()
                timestamp = generate_rotation_timestamp()
                rotated = path.with_name(f"{path.name}.{timestamp}")
                path.rename(rotated)
                cleanup_old_rotated_files(path.parent, bound_max_rotated, base_name=path.name)
                state["file"] = open(bound_path, "a")
                state["size"] = 0

    def sink(message: Any) -> None:
        record = message.record
        event = _build_flat_log_dict(record, bound_type, bound_source, bound_command)
        json_line = json.dumps(event, separators=(",", ":"), default=str) + "\n"
        line_bytes = len(json_line.encode("utf-8"))

        _rotate_if_needed()
        fh = _ensure_file()
        fh.write(json_line)
        fh.flush()
        state["size"] += line_bytes

    return sink


# Default cap for rotating raw-line capture files (subprocess stdout/stderr that
# can't go through the structured JSONL path). 50 MiB, keeping 10 copies, bounds
# a single such log at ~500 MiB.
_DEFAULT_ROTATING_LINE_MAX_BYTES: Final[int] = 50 * 1024 * 1024


class RotatingLineWriter(MutableModel):
    """Append raw text lines to a size-rotated, optionally timestamped log file.

    Built for capturing the stdout/stderr of subprocesses that emit
    unstructured text (e.g. the Latchkey gateway), which cannot go through the
    structured loguru/JSONL path. Each line is optionally prefixed with an
    ISO-8601 UTC timestamp (nanosecond precision, matching
    :func:`format_nanosecond_iso_timestamp`) so the timing of otherwise opaque
    output is observable. The file is rotated once it would exceed
    ``max_size_bytes``; at most ``max_rotated_count`` rotated copies are kept,
    named ``<name>.<rotation_timestamp>`` (the same scheme
    :func:`make_jsonl_file_sink` uses), and the oldest are pruned on rotation.

    Thread-safe: :meth:`write_line` serializes writes (and rotation) under an
    internal lock, so it can be used directly as a per-line subprocess-output
    callback invoked from multiple reader threads.
    """

    path: Path = Field(frozen=True, description="On-disk file that raw lines are appended to.")
    max_size_bytes: int = Field(
        default=_DEFAULT_ROTATING_LINE_MAX_BYTES,
        frozen=True,
        description="Rotate the file once writing the next line would push it past this many bytes.",
    )
    max_rotated_count: int = Field(
        default=10,
        frozen=True,
        description="Maximum number of rotated copies to keep; the oldest are pruned on rotation.",
    )
    is_timestamped: bool = Field(
        default=True,
        frozen=True,
        description="Prefix each written line with an ISO-8601 UTC nanosecond timestamp.",
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _file: IO[bytes] | None = PrivateAttr(default=None)
    _size: int = PrivateAttr(default=0)

    def write_line(self, line: str) -> None:
        """Append ``line`` (one log line) to the file, rotating first if needed.

        A trailing newline is normalized so exactly one is written. Write
        failures are swallowed after a warning so a logging hiccup never crashes
        the caller's output-pump thread.
        """
        text = line.rstrip("\n")
        if self.is_timestamped:
            text = f"{format_nanosecond_iso_timestamp(datetime.now(timezone.utc))} {text}"
        data = (text + "\n").encode("utf-8", errors="replace")
        with self._lock:
            try:
                self._rotate_if_needed_locked(len(data))
                handle = self._ensure_open_locked()
                handle.write(data)
                handle.flush()
                self._size += len(data)
            except OSError as e:
                logger.warning("Failed to write rotating log line to {}: {}", self.path, e)

    def close(self) -> None:
        """Close the underlying file handle (idempotent)."""
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None

    def _ensure_open_locked(self) -> IO[bytes]:
        if self._file is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            cleanup_old_rotated_files(self.path.parent, self.max_rotated_count, base_name=self.path.name)
            self._file = self.path.open("ab")
            try:
                self._size = self.path.stat().st_size
            except OSError:
                self._size = 0
        return self._file

    def _rotate_if_needed_locked(self, incoming_bytes: int) -> None:
        # Open lazily first so a freshly-constructed writer learns the on-disk size.
        self._ensure_open_locked()
        if self._size + incoming_bytes < self.max_size_bytes:
            return
        with rotation_lock(self.path.parent):
            # Re-check actual size: another writer/process may have already rotated.
            try:
                actual_size = self.path.stat().st_size
            except OSError:
                actual_size = 0
            if actual_size < self.max_size_bytes:
                # Already rotated elsewhere -- reopen our handle onto the fresh file.
                if self._file is not None:
                    self._file.close()
                self._file = self.path.open("ab")
                self._size = actual_size
                return
            if self._file is not None:
                self._file.close()
                self._file = None
            rotated = self.path.with_name(f"{self.path.name}.{generate_rotation_timestamp()}")
            self.path.rename(rotated)
            cleanup_old_rotated_files(self.path.parent, self.max_rotated_count, base_name=self.path.name)
            self._file = self.path.open("ab")
            self._size = 0
