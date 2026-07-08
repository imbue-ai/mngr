"""Tests for the shared color-enable policy."""

import io

import pytest

from imbue.mngr.colors import should_use_color
from imbue.mngr.utils.testing import FakeTtyStream


def test_should_use_color_returns_false_when_no_color_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """should_use_color should return False when NO_COLOR is set."""
    monkeypatch.setenv("NO_COLOR", "")
    assert should_use_color(FakeTtyStream()) is False


def test_should_use_color_returns_false_when_not_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """should_use_color should return False when the stream is not a TTY."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert should_use_color(io.StringIO()) is False


def test_should_use_color_returns_true_when_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """should_use_color should return True when the stream is a TTY and NO_COLOR is not set."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert should_use_color(FakeTtyStream()) is True
