"""Tests for pure helpers in the imbue_cloud Host module."""

from datetime import datetime
from datetime import timezone

from imbue.mngr_imbue_cloud.host import _parse_create_time


def test_parse_create_time_parses_valid_iso() -> None:
    value = "2025-03-04T05:06:07+00:00"
    assert _parse_create_time(value) == datetime(2025, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


def test_parse_create_time_falls_back_on_missing() -> None:
    """A missing create_time yields a usable (now) timestamp rather than crashing."""
    before = datetime.now(timezone.utc)
    result = _parse_create_time(None)
    assert result.tzinfo is not None
    assert result >= before


def test_parse_create_time_falls_back_on_malformed() -> None:
    before = datetime.now(timezone.utc)
    result = _parse_create_time("not-a-timestamp")
    assert result.tzinfo is not None
    assert result >= before
