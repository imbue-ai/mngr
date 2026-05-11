"""Tests for pydantic-related utility helpers."""

import typing

from imbue.mngr.utils.pydantic_utils import unwrap_optional


def test_unwrap_optional_pep604_union_with_none() -> None:
    """`X | None` unwraps to `X`."""
    assert unwrap_optional(int | None) is int
    assert unwrap_optional(str | None) is str


def test_unwrap_optional_non_union_passthrough() -> None:
    """Non-union annotations are returned unchanged."""
    assert unwrap_optional(int) is int
    assert unwrap_optional(str) is str
    assert unwrap_optional(list[int]) == list[int]


def test_unwrap_optional_multi_arg_union_unchanged() -> None:
    """A Union with more than one non-None arg is not collapsed."""
    annotation = int | str | None
    assert unwrap_optional(annotation) == annotation


def test_unwrap_optional_union_without_none_unchanged() -> None:
    """A Union with no None arm is returned as-is."""
    annotation = int | str
    assert unwrap_optional(annotation) == annotation


def test_unwrap_optional_none_only() -> None:
    """`None` alone (not a Union) is returned as-is."""
    assert unwrap_optional(type(None)) is type(None)


def test_unwrap_optional_get_origin_of_result_matches_inner() -> None:
    """After unwrap, `typing.get_origin` of a parameterized inner is preserved."""
    unwrapped = unwrap_optional(list[int] | None)
    assert typing.get_origin(unwrapped) is list
