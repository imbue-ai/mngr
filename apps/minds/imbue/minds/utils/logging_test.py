import io
import json
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from imbue.minds.utils.logging import ConsoleLogLevel
from imbue.minds.utils.logging import _format_user_message
from imbue.minds.utils.logging import console_level_from_verbose_and_quiet
from imbue.minds.utils.logging import install_exception_dedup_patcher
from imbue.minds.utils.logging import is_not_duplicate_exception
from imbue.minds.utils.logging import setup_logging


def test_default_level_is_info() -> None:
    level = console_level_from_verbose_and_quiet(verbose=0, quiet=False)

    assert level == ConsoleLogLevel.INFO


def test_single_verbose_gives_debug() -> None:
    level = console_level_from_verbose_and_quiet(verbose=1, quiet=False)

    assert level == ConsoleLogLevel.DEBUG


def test_double_verbose_gives_trace() -> None:
    level = console_level_from_verbose_and_quiet(verbose=2, quiet=False)

    assert level == ConsoleLogLevel.TRACE


def test_triple_verbose_gives_trace() -> None:
    level = console_level_from_verbose_and_quiet(verbose=3, quiet=False)

    assert level == ConsoleLogLevel.TRACE


def test_quiet_gives_none() -> None:
    level = console_level_from_verbose_and_quiet(verbose=0, quiet=True)

    assert level == ConsoleLogLevel.NONE


def test_quiet_overrides_verbose() -> None:
    level = console_level_from_verbose_and_quiet(verbose=2, quiet=True)

    assert level == ConsoleLogLevel.NONE


class _FakeLevel:
    """Fake loguru level object for testing format functions."""

    def __init__(self, name: str) -> None:
        self.name = name


def _make_fake_record(level_name: str) -> dict[str, _FakeLevel]:
    """Create a minimal dict matching the loguru record shape for _format_user_message."""
    return {"level": _FakeLevel(level_name)}


def test_format_user_message_info_returns_plain_format() -> None:
    result = _format_user_message(_make_fake_record("INFO"))
    assert result == "{message}\n"


def test_format_user_message_warning_includes_prefix() -> None:
    result = _format_user_message(_make_fake_record("WARNING"))
    assert "WARNING:" in result
    assert "{message}" in result


def test_format_user_message_error_includes_prefix() -> None:
    result = _format_user_message(_make_fake_record("ERROR"))
    assert "ERROR:" in result
    assert "{message}" in result


def test_format_user_message_debug_includes_message_placeholder() -> None:
    result = _format_user_message(_make_fake_record("DEBUG"))
    assert "{message}" in result


def test_format_user_message_trace_includes_message_placeholder() -> None:
    result = _format_user_message(_make_fake_record("TRACE"))
    assert "{message}" in result


@pytest.mark.usefixtures("_isolated_logger")
def test_setup_logging_none_suppresses_output(capfd: Any) -> None:
    setup_logging(ConsoleLogLevel.NONE)

    logger.info("suppressed-marker-82734")

    captured = capfd.readouterr()
    assert "suppressed-marker-82734" not in captured.err


@pytest.mark.usefixtures("_isolated_logger")
@pytest.mark.parametrize(
    "level, loguru_level, marker",
    [
        (ConsoleLogLevel.INFO, "INFO", "info-marker-91827"),
        (ConsoleLogLevel.DEBUG, "DEBUG", "debug-marker-73829"),
        (ConsoleLogLevel.TRACE, "TRACE", "trace-marker-28374"),
        (ConsoleLogLevel.WARN, "WARNING", "warn-marker-92837"),
        (ConsoleLogLevel.ERROR, "ERROR", "error-marker-83729"),
    ],
)
def test_setup_logging_shows_messages_at_configured_level(
    level: ConsoleLogLevel,
    loguru_level: str,
    marker: str,
    capfd: Any,
) -> None:
    """Verify that setup_logging configures loguru to emit messages at the given level."""
    setup_logging(level)

    logger.log(loguru_level, marker)

    captured = capfd.readouterr()
    assert marker in captured.err


@pytest.mark.usefixtures("_isolated_logger")
def test_setup_logging_always_uses_human_format_on_stderr(capfd: Any) -> None:
    """Stderr output is always human-readable, never JSONL."""
    setup_logging(ConsoleLogLevel.INFO)

    logger.info("human-test-marker-71824")

    captured = capfd.readouterr()
    assert "human-test-marker-71824" in captured.err
    # Should not be JSON
    assert "{" not in captured.err.split("human-test-marker-71824")[0]


@pytest.mark.usefixtures("_isolated_logger")
def test_setup_logging_with_log_file(tmp_path: Path, capfd: Any) -> None:
    """Verify that --log-file creates a JSONL log file."""
    log_file = tmp_path / "test.jsonl"
    setup_logging(ConsoleLogLevel.INFO, log_file=log_file)

    logger.info("file-log-test-marker-38291")

    captured = capfd.readouterr()
    # Still shows on stderr
    assert "file-log-test-marker-38291" in captured.err

    # Also written to the log file
    log_content = log_file.read_text()
    assert log_content.strip()
    event = json.loads(log_content.strip().split("\n")[-1])
    assert event["message"] == "file-log-test-marker-38291"
    assert event["type"] == "minds"


def _make_dedup_buffer() -> io.StringIO:
    """Add a dedup-filtered in-memory sink and return its buffer.

    Assumes the caller has already removed existing handlers (via ``_isolated_logger``) and installs
    the dedup patcher so the sink participates in exception-report dedup.
    """
    install_exception_dedup_patcher()
    buffer = io.StringIO()
    logger.add(
        buffer,
        level="DEBUG",
        format="{level}|{message}",
        filter=is_not_duplicate_exception,
        colorize=False,
        diagnose=False,
        backtrace=False,
    )
    return buffer


@pytest.mark.usefixtures("_isolated_logger")
def test_same_exception_reported_at_error_only_logs_once() -> None:
    buffer = _make_dedup_buffer()
    error = ValueError("boom-marker-55512")

    for attempt in range(3):
        logger.opt(exception=error).error("attempt {}", attempt)

    output = buffer.getvalue()
    assert "attempt 0" in output
    assert "attempt 1" not in output
    assert "attempt 2" not in output


@pytest.mark.usefixtures("_isolated_logger")
def test_distinct_exception_instances_each_report() -> None:
    buffer = _make_dedup_buffer()

    logger.opt(exception=ValueError("first-marker-22841")).error("first-error")
    logger.opt(exception=RuntimeError("second-marker-22842")).error("second-error")

    output = buffer.getvalue()
    assert "first-error" in output
    assert "second-error" in output


@pytest.mark.usefixtures("_isolated_logger")
def test_exception_below_error_level_is_not_deduped() -> None:
    buffer = _make_dedup_buffer()
    error = ValueError("warn-boom-90013")

    # Logging the same exception at warning level must never mark it as reported, so a later
    # genuine error report for the same instance still goes through.
    logger.opt(exception=error).warning("warn-one")
    logger.opt(exception=error).warning("warn-two")
    logger.opt(exception=error).error("error-after-warnings")

    output = buffer.getvalue()
    assert "warn-one" in output
    assert "warn-two" in output
    assert "error-after-warnings" in output


@pytest.mark.usefixtures("_isolated_logger")
def test_dedup_decision_is_consistent_across_sinks_in_one_emit() -> None:
    install_exception_dedup_patcher()
    first_buffer = io.StringIO()
    second_buffer = io.StringIO()
    for buffer in (first_buffer, second_buffer):
        logger.add(
            buffer,
            level="DEBUG",
            format="{message}",
            filter=is_not_duplicate_exception,
            colorize=False,
            diagnose=False,
            backtrace=False,
        )
    error = ValueError("multi-sink-30021")

    logger.opt(exception=error).error("first-emit")
    logger.opt(exception=error).error("second-emit")

    # The first emit reaches every sink; the second (duplicate) reaches none.
    for buffer in (first_buffer, second_buffer):
        assert "first-emit" in buffer.getvalue()
        assert "second-emit" not in buffer.getvalue()


@pytest.mark.usefixtures("_isolated_logger")
def test_setup_logging_dedups_repeated_exception_on_stderr(capfd: Any) -> None:
    setup_logging(ConsoleLogLevel.INFO)
    error = ValueError("setup-dedup-44190")

    logger.opt(exception=error).error("setup-first-marker")
    logger.opt(exception=error).error("setup-second-marker")

    captured = capfd.readouterr()
    assert "setup-first-marker" in captured.err
    assert "setup-second-marker" not in captured.err
