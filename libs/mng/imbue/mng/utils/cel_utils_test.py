"""Tests for CEL filter utilities."""

import pytest

from imbue.mng.errors import MngError
from imbue.mng.utils.cel_utils import apply_cel_filters_to_context
from imbue.mng.utils.cel_utils import compile_cel_filters


def test_cel_string_contains_method() -> None:
    """CEL string contains() should work on context values."""
    includes, excludes = compile_cel_filters(
        include_filters=('name.contains("prod")',),
        exclude_filters=(),
    )
    matches = apply_cel_filters_to_context(
        context={"name": "my-prod-agent"},
        include_filters=includes,
        exclude_filters=excludes,
        error_context_description="test",
    )
    assert matches is True

    no_match = apply_cel_filters_to_context(
        context={"name": "my-dev-agent"},
        include_filters=includes,
        exclude_filters=excludes,
        error_context_description="test",
    )
    assert no_match is False


def test_cel_string_starts_with_method() -> None:
    """CEL string startsWith() should work on context values."""
    includes, excludes = compile_cel_filters(
        include_filters=('name.startsWith("staging-")',),
        exclude_filters=(),
    )
    matches = apply_cel_filters_to_context(
        context={"name": "staging-app"},
        include_filters=includes,
        exclude_filters=excludes,
        error_context_description="test",
    )
    assert matches is True

    no_match = apply_cel_filters_to_context(
        context={"name": "prod-app"},
        include_filters=includes,
        exclude_filters=excludes,
        error_context_description="test",
    )
    assert no_match is False


def test_cel_string_ends_with_method() -> None:
    """CEL string endsWith() should work on context values."""
    includes, excludes = compile_cel_filters(
        include_filters=('name.endsWith("-dev")',),
        exclude_filters=(),
    )
    matches = apply_cel_filters_to_context(
        context={"name": "myapp-dev"},
        include_filters=includes,
        exclude_filters=excludes,
        error_context_description="test",
    )
    assert matches is True


def test_cel_invalid_include_filter_raises_mng_error() -> None:
    """compile_cel_filters should raise MngError for invalid include filter."""
    with pytest.raises(MngError, match="Invalid include filter"):
        compile_cel_filters(
            include_filters=("this is not valid cel @@@@",),
            exclude_filters=(),
        )


def test_cel_invalid_exclude_filter_raises_mng_error() -> None:
    """compile_cel_filters should raise MngError for invalid exclude filter."""
    with pytest.raises(MngError, match="Invalid exclude filter"):
        compile_cel_filters(
            include_filters=(),
            exclude_filters=("this is not valid cel @@@@",),
        )


def test_cel_include_filter_eval_error_returns_false() -> None:
    """apply_cel_filters_to_context should return False if include filter errors."""
    # Compile a filter that references a field not in the context
    includes, excludes = compile_cel_filters(
        include_filters=('nonexistent_field == "value"',),
        exclude_filters=(),
    )
    result = apply_cel_filters_to_context(
        context={"name": "test"},
        include_filters=includes,
        exclude_filters=excludes,
        error_context_description="test-agent",
    )
    assert result is False


def test_cel_exclude_filter_eval_error_continues() -> None:
    """apply_cel_filters_to_context should continue when exclude filter errors."""
    # Compile an exclude filter that references a field not in the context
    includes, excludes = compile_cel_filters(
        include_filters=(),
        exclude_filters=('nonexistent_field == "value"',),
    )
    result = apply_cel_filters_to_context(
        context={"name": "test"},
        include_filters=includes,
        exclude_filters=excludes,
        error_context_description="test-agent",
    )
    # Should return True since the exclude filter errored and was skipped
    assert result is True


def test_cel_exclude_filter_matches_returns_false() -> None:
    """apply_cel_filters_to_context should return False when exclude filter matches."""
    includes, excludes = compile_cel_filters(
        include_filters=(),
        exclude_filters=('name == "excluded-agent"',),
    )
    result = apply_cel_filters_to_context(
        context={"name": "excluded-agent"},
        include_filters=includes,
        exclude_filters=excludes,
        error_context_description="test",
    )
    assert result is False


def test_cel_exclude_filter_no_match_returns_true() -> None:
    """apply_cel_filters_to_context should return True when exclude filter doesn't match."""
    includes, excludes = compile_cel_filters(
        include_filters=(),
        exclude_filters=('name == "excluded-agent"',),
    )
    result = apply_cel_filters_to_context(
        context={"name": "included-agent"},
        include_filters=includes,
        exclude_filters=excludes,
        error_context_description="test",
    )
    assert result is True


def test_cel_nested_dict_dot_notation() -> None:
    """CEL filters should support dot notation for nested dicts."""
    includes, excludes = compile_cel_filters(
        include_filters=('host.provider == "docker"',),
        exclude_filters=(),
    )
    result = apply_cel_filters_to_context(
        context={"host": {"provider": "docker", "name": "my-host"}},
        include_filters=includes,
        exclude_filters=excludes,
        error_context_description="test",
    )
    assert result is True
