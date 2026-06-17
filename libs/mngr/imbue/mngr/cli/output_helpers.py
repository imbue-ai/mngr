import json
import string
import sys
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import assert_never

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import OutputFormat


def write_json_line(data: Mapping[str, Any]) -> None:
    """Write a JSON object as a line to stdout.

    Used for both JSON output (a single terminating object) and JSONL output
    (one object per line, streamed) where we need raw JSON without any logger
    formatting. Sibling to ``write_human_line``.
    """
    sys.stdout.write(json.dumps(data) + "\n")
    sys.stdout.flush()


def write_human_line(message: str, *args: Any) -> None:
    """Write a human-readable output line to stdout.

    Use this for actual command output (results, tables, status messages) in HUMAN format.
    For log/diagnostic messages, use logger.* instead (which goes to stderr).
    Accepts positional format args like loguru: write_human_line("Created {} items", count).
    """
    if args:
        formatted = message.format(*args)
    else:
        formatted = message
    sys.stdout.write(formatted + "\n")
    sys.stdout.flush()


def write_command_stdout_and_stderr(stdout: str, stderr: str) -> None:
    """Write a captured command's stdout and stderr to the user's stdout/stderr.

    Used by ``mngr exec`` and friends to forward raw command output to the
    invoking shell. Adds a trailing newline if the captured output didn't end
    with one (so subsequent prompts/output don't get glued onto the last line).
    """
    if stdout:
        sys.stdout.write(stdout)
        if not stdout.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
    if stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith("\n"):
            sys.stderr.write("\n")
        sys.stderr.flush()


@pure
def format_size(size_bytes: int) -> str:
    """Format bytes into a human-readable size string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024**2:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024**3:
        return f"{size_bytes / 1024**2:.1f} MB"
    if size_bytes < 1024**4:
        return f"{size_bytes / 1024**3:.2f} GB"
    return f"{size_bytes / 1024**4:.2f} TB"


def read_tty_choice(prompt: str) -> str:
    """Read a single line from /dev/tty (works even when stdin is piped).

    When a script is invoked via ``curl ... | bash``, stdin is the pipe
    from curl, not the terminal.  This function opens /dev/tty directly
    so interactive prompts still work.  Returns an empty string if
    /dev/tty is unavailable (e.g. in CI).
    """
    try:
        with open("/dev/tty") as tty:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            return tty.readline().strip()
    except OSError:
        return ""


class AbortError(BaseException):
    """Exception raised when error behavior is ABORT.

    Inherits from BaseException (not Exception) so it cannot be caught
    by generic Exception handlers, ensuring it propagates to the top level.
    """

    def __init__(
        self,
        message: str,
        # The original exception that caused the abort, if any
        original_exception: Exception | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.original_exception = original_exception


def emit_info(message: str, output_format: OutputFormat) -> None:
    """Emit an informational message in the appropriate format."""
    match output_format:
        case OutputFormat.HUMAN:
            write_human_line(message)
        case OutputFormat.JSONL:
            event = {"event": "info", "message": message}
            write_json_line(event)
        case OutputFormat.JSON:
            # JSON mode: silent until final output
            pass
        case _ as unreachable:
            assert_never(unreachable)


def emit_event(
    # The type of event (e.g., "destroyed", "created")
    event_type: str,
    # Event data dictionary. For HUMAN format, should include "message" key.
    data: Mapping[str, Any],
    output_format: OutputFormat,
) -> None:
    """Emit an event in the appropriate format."""
    match output_format:
        case OutputFormat.HUMAN:
            if "message" in data:
                write_human_line(str(data["message"]))
        case OutputFormat.JSONL:
            event = {"event": event_type, **data}
            write_json_line(event)
        case OutputFormat.JSON:
            # JSON mode: silent until final output
            pass
        case _ as unreachable:
            assert_never(unreachable)


def emit_operator_result(
    event_name: str,
    data: Mapping[str, Any],
    output_format: OutputFormat,
    human_lines: Sequence[str],
) -> None:
    """Emit an operator-command result (e.g. provider ``prepare`` / ``cleanup``) in the requested format.

    Centralizes the format dispatch the provider operator commands share: JSON
    writes ``data`` as one object, JSONL emits a ``<event_name>`` event carrying
    ``data``, and HUMAN writes each already-formatted line in ``human_lines``.
    Each provider still owns its ``data`` dict and its human wording (the caller
    builds ``human_lines``) -- only the format switch lives here.
    """
    match output_format:
        case OutputFormat.JSON:
            write_json_line(data)
        case OutputFormat.JSONL:
            emit_event(event_name, data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            for line in human_lines:
                write_human_line(line)
        case _ as unreachable:
            assert_never(unreachable)


def on_error(
    error_msg: str,
    # How to handle the error: ABORT raises AbortError, CONTINUE logs and continues
    error_behavior: ErrorBehavior,
    output_format: OutputFormat,
    # Optional exception that caused the error
    exc: Exception | None = None,
) -> None:
    """Handle an error by emitting it and optionally aborting."""
    # Emit the error in the appropriate format
    match output_format:
        case OutputFormat.HUMAN:
            logger.error(error_msg)
        case OutputFormat.JSONL:
            error_event: dict[str, Any] = {"event": "error", "message": error_msg}
            if exc is not None:
                error_event["error_class"] = type(exc).__name__
            write_json_line(error_event)
        case OutputFormat.JSON:
            # JSON mode: errors collected and shown in final output
            pass
        case _ as unreachable:
            assert_never(unreachable)

    # Abort if requested
    if error_behavior == ErrorBehavior.ABORT:
        raise AbortError(error_msg, original_exception=exc)


def emit_error_event(error: BaseException, output_format: OutputFormat | None) -> None:
    """Emit a machine-readable JSONL error record so subprocess callers can detect the error type.

    No-op unless ``output_format`` is JSONL. Called from the top-level CLI
    exception handler so every command surfaces a structured
    ``{"event": "error", "error_class": ..., "message": ...}`` line in
    ``--format jsonl`` mode -- letting callers (e.g. minds) branch on the
    exception *type* without parsing human-formatted error text.
    """
    if output_format is not OutputFormat.JSONL:
        return
    write_json_line({"event": "error", "error_class": type(error).__name__, "message": str(error)})


@pure
def render_format_template(template: str, values: Mapping[str, str]) -> str:
    """Expand a str.format()-style template using field values from a mapping.

    Uses string.Formatter().parse() to extract field names, resolves each via
    mapping lookup, then assembles the output. This avoids str.format_map()
    because Python's format machinery interprets dots as attribute access, but
    our field names may use dots as part of the key path.
    """
    parts: list[str] = []
    for literal_text, field_name, format_spec, conversion in string.Formatter().parse(template):
        parts.append(literal_text)
        if field_name is None:
            continue
        value = values.get(field_name, "")
        if conversion is None:
            pass
        elif conversion == "s":
            value = str(value)
        elif conversion == "r":
            value = repr(value)
        elif conversion == "a":
            value = ascii(value)
        else:
            raise AssertionError(f"Unknown conversion: {conversion!r}")
        if format_spec:
            value = format(value, format_spec)
        parts.append(value)
    return "".join(parts)


def emit_format_template_lines(
    template: str,
    items: Sequence[Mapping[str, str]],
) -> None:
    """Emit one line per item using a format template string."""
    for item in items:
        line = render_format_template(template, item)
        sys.stdout.write(line + "\n")
    sys.stdout.flush()
