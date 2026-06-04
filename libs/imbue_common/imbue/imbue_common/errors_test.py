"""Tests for errors module."""

import pytest

from imbue.imbue_common.errors import SwitchError


def test_switch_error_is_raisable_and_catchable_with_message() -> None:
    """SwitchError should behave as an Exception subclass carrying its message."""
    assert issubclass(SwitchError, Exception)
    with pytest.raises(SwitchError, match="unexpected branch"):
        raise SwitchError("unexpected branch")
