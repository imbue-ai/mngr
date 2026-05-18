"""Tests for env_vars helpers."""

import pytest

from imbue.imbue_common.env_vars import parse_int_env


def test_parse_int_env_returns_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNGR_TEST_INT_PARSER", raising=False)
    assert parse_int_env("MNGR_TEST_INT_PARSER", 7) == 7


def test_parse_int_env_returns_default_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_TEST_INT_PARSER", "")
    assert parse_int_env("MNGR_TEST_INT_PARSER", 3) == 3


def test_parse_int_env_returns_default_when_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_TEST_INT_PARSER", "not-a-number")
    assert parse_int_env("MNGR_TEST_INT_PARSER", 5) == 5


def test_parse_int_env_parses_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_TEST_INT_PARSER", "42")
    assert parse_int_env("MNGR_TEST_INT_PARSER", 0) == 42


def test_parse_int_env_parses_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_TEST_INT_PARSER", "-3")
    assert parse_int_env("MNGR_TEST_INT_PARSER", 0) == -3


def test_parse_int_env_parses_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_TEST_INT_PARSER", "0")
    assert parse_int_env("MNGR_TEST_INT_PARSER", 99) == 0
