import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from inline_snapshot import snapshot
from loguru import logger

from imbue.minds.utils.logging import ConsoleLogLevel
from imbue.minds.utils.logging import _format_user_message
from imbue.minds.utils.logging import console_level_from_verbose_and_quiet
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


def _make_fake_record(level_name: str) -> dict[str, Any]:
    """Create a minimal dict matching the loguru record shape for _format_user_message."""
    return {"level": SimpleNamespace(name=level_name)}


def test_format_user_message_info_returns_plain_format() -> None:
    result = _format_user_message(_make_fake_record("INFO"))
    assert result == snapshot("{message}\n")


def test_format_user_message_warning_wraps_prefixed_message_in_color() -> None:
    result = _format_user_message(_make_fake_record("WARNING"))
    assert result == snapshot("\x1b[1;38;5;178mWARNING: {message}\x1b[0m\n")


def test_format_user_message_error_wraps_prefixed_message_in_color() -> None:
    result = _format_user_message(_make_fake_record("ERROR"))
    assert result == snapshot("\x1b[1;38;5;196mERROR: {message}\x1b[0m\n")


def test_format_user_message_debug_wraps_message_in_color() -> None:
    result = _format_user_message(_make_fake_record("DEBUG"))
    assert result == snapshot("\x1b[38;5;33m{message}\x1b[0m\n")


def test_format_user_message_trace_wraps_message_in_color() -> None:
    result = _format_user_message(_make_fake_record("TRACE"))
    assert result == snapshot("\x1b[38;5;99m{message}\x1b[0m\n")


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
@pytest.mark.parametrize(
    "configured_level, suppressed_loguru_level, marker",
    [
        (ConsoleLogLevel.INFO, "DEBUG", "below-info-marker-44910"),
        (ConsoleLogLevel.INFO, "TRACE", "below-info-trace-marker-44911"),
        (ConsoleLogLevel.DEBUG, "TRACE", "below-debug-marker-44912"),
        (ConsoleLogLevel.WARN, "INFO", "below-warn-marker-44913"),
        (ConsoleLogLevel.ERROR, "WARNING", "below-error-marker-44914"),
    ],
)
def test_setup_logging_suppresses_messages_below_configured_level(
    configured_level: ConsoleLogLevel,
    suppressed_loguru_level: str,
    marker: str,
    capfd: Any,
) -> None:
    """Messages below the configured threshold must not reach stderr."""
    setup_logging(configured_level)

    logger.log(suppressed_loguru_level, marker)

    captured = capfd.readouterr()
    assert marker not in captured.err


@pytest.mark.usefixtures("_isolated_logger")
def test_setup_logging_always_uses_human_format_on_stderr(capfd: Any) -> None:
    """Stderr output is always the plain human format, never JSONL."""
    setup_logging(ConsoleLogLevel.INFO)

    logger.info("human-test-marker-71824")

    captured = capfd.readouterr()
    # The INFO human format is exactly `{message}\n`, so stderr is just the marker line.
    assert captured.err == "human-test-marker-71824\n"


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
