"""Unit tests for the key-suffix operator helpers."""

from typing import Any

import pytest

from imbue.overlay.errors import OverlayError
from imbue.overlay.operators import ASSIGN_SUFFIX
from imbue.overlay.operators import EXTEND_SUFFIX
from imbue.overlay.operators import assign_bare_key
from imbue.overlay.operators import bare_key
from imbue.overlay.operators import check_no_conflicting_assign
from imbue.overlay.operators import is_assign_key
from imbue.overlay.operators import is_extend_key
from imbue.overlay.operators import parse_scalar_value

# =============================================================================
# is_extend_key / bare_key
# =============================================================================


def test_is_extend_key_recognises_suffix() -> None:
    assert is_extend_key("cli_args__extend")
    assert is_extend_key("a__extend")


def test_is_extend_key_rejects_bare_suffix() -> None:
    """A bare ``__extend`` (no preceding field name) is not a valid extend key."""
    assert not is_extend_key(EXTEND_SUFFIX)


def test_is_extend_key_rejects_plain_field() -> None:
    assert not is_extend_key("cli_args")
    assert not is_extend_key("")


def test_bare_key_strips_suffix() -> None:
    assert bare_key("cli_args__extend") == "cli_args"
    assert bare_key("a__extend") == "a"


# =============================================================================
# is_assign_key / assign_bare_key
# =============================================================================


def test_is_assign_key_recognises_suffix() -> None:
    assert is_assign_key("permissions__assign")
    assert is_assign_key("a__assign")


def test_is_assign_key_rejects_bare_suffix() -> None:
    assert not is_assign_key(ASSIGN_SUFFIX)


def test_is_assign_key_rejects_plain_field() -> None:
    assert not is_assign_key("permissions")
    assert not is_assign_key("permissions__extend")


def test_assign_bare_key_strips_suffix() -> None:
    assert assign_bare_key("permissions__assign") == "permissions"


# =============================================================================
# parse_scalar_value
# =============================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("false", False),
        ("42", 42),
        ("3.14", 3.14),
        ('"quoted"', "quoted"),
        ("[1, 2, 3]", [1, 2, 3]),
        ('{"k": "v"}', {"k": "v"}),
        ("not_json", "not_json"),
        ("", ""),
    ],
)
def test_parse_scalar_value(raw: str, expected: Any) -> None:
    """JSON-parses first, falls back to the raw string when not valid JSON."""
    assert parse_scalar_value(raw) == expected


# =============================================================================
# check_no_conflicting_assign
# =============================================================================


def test_check_no_conflicting_assign_passes_without_conflict() -> None:
    check_no_conflicting_assign({"a": 1, "b__extend": [2], "c__assign": 3})


def test_check_no_conflicting_assign_raises_on_bare_plus_assign() -> None:
    with pytest.raises(OverlayError, match="Conflicting assignment"):
        check_no_conflicting_assign({"model": "opus", "model__assign": "sonnet"})


def test_check_no_conflicting_assign_includes_field_path_in_message() -> None:
    with pytest.raises(OverlayError, match="at 'permissions'"):
        check_no_conflicting_assign({"f": 1, "f__assign": 2}, "permissions")
