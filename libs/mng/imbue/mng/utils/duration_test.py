"""Unit tests for duration parsing utility."""

import pytest

from imbue.mng.errors import UserInputError
from imbue.mng.utils.duration import parse_duration_to_seconds


def test_parse_duration_days() -> None:
    assert parse_duration_to_seconds("7d") == 604800.0
    assert parse_duration_to_seconds("1d") == 86400.0


def test_parse_duration_hours() -> None:
    assert parse_duration_to_seconds("24h") == 86400.0
    assert parse_duration_to_seconds("1h") == 3600.0


def test_parse_duration_minutes() -> None:
    assert parse_duration_to_seconds("30m") == 1800.0
    assert parse_duration_to_seconds("1m") == 60.0


def test_parse_duration_seconds() -> None:
    assert parse_duration_to_seconds("90s") == 90.0
    assert parse_duration_to_seconds("1s") == 1.0


def test_parse_duration_combined() -> None:
    assert parse_duration_to_seconds("1h30m") == 5400.0
    assert parse_duration_to_seconds("1d12h") == 129600.0
    assert parse_duration_to_seconds("1d2h30m10s") == 95410.0


def test_parse_duration_case_insensitive() -> None:
    assert parse_duration_to_seconds("7D") == 604800.0
    assert parse_duration_to_seconds("24H") == 86400.0
    assert parse_duration_to_seconds("30M") == 1800.0
    assert parse_duration_to_seconds("90S") == 90.0


def test_parse_duration_with_whitespace() -> None:
    assert parse_duration_to_seconds("  7d  ") == 604800.0


def test_parse_duration_empty_string_raises() -> None:
    with pytest.raises(UserInputError, match="empty string"):
        parse_duration_to_seconds("")


def test_parse_duration_invalid_format_raises() -> None:
    with pytest.raises(UserInputError, match="Invalid duration"):
        parse_duration_to_seconds("abc")


def test_parse_duration_zero_raises() -> None:
    with pytest.raises(UserInputError, match="greater than zero"):
        parse_duration_to_seconds("0d")


def test_parse_duration_plain_integer_treated_as_seconds() -> None:
    assert parse_duration_to_seconds("300") == 300.0
    assert parse_duration_to_seconds("42") == 42.0
    assert parse_duration_to_seconds("1") == 1.0


def test_parse_duration_plain_integer_with_whitespace() -> None:
    assert parse_duration_to_seconds("  300  ") == 300.0


def test_parse_duration_plain_zero_raises() -> None:
    with pytest.raises(UserInputError, match="greater than zero"):
        parse_duration_to_seconds("0")


def test_parse_duration_negative_integer_raises() -> None:
    with pytest.raises(UserInputError, match="greater than zero"):
        parse_duration_to_seconds("-5")
