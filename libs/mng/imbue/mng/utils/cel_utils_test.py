"""Tests for CEL utilities."""

import pytest

from imbue.mng.errors import MngError
from imbue.mng.utils.cel_utils import apply_cel_filters_to_context
from imbue.mng.utils.cel_utils import build_cel_context
from imbue.mng.utils.cel_utils import compile_cel_filters
from imbue.mng.utils.cel_utils import compile_cel_sort_keys
from imbue.mng.utils.cel_utils import evaluate_cel_sort_key
from imbue.mng.utils.cel_utils import parse_cel_sort_spec


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


# =============================================================================
# Tests for build_cel_context
# =============================================================================


def test_build_cel_context_converts_string_values() -> None:
    """build_cel_context should convert raw string values to CEL-compatible values."""
    raw = {"name": "test-agent", "state": "running"}
    cel_ctx = build_cel_context(raw)
    assert "name" in cel_ctx
    assert "state" in cel_ctx


def test_build_cel_context_converts_nested_dicts() -> None:
    """build_cel_context should convert nested dicts for dot notation support."""
    raw = {"host": {"provider": "local", "name": "my-host"}}
    cel_ctx = build_cel_context(raw)
    assert "host" in cel_ctx


# =============================================================================
# Tests for parse_cel_sort_spec
# =============================================================================


def test_parse_cel_sort_spec_simple_field() -> None:
    """parse_cel_sort_spec should parse a simple field name as ascending."""
    result = parse_cel_sort_spec("name")
    assert result == [("name", False)]


def test_parse_cel_sort_spec_with_asc() -> None:
    """parse_cel_sort_spec should parse explicit ascending direction."""
    result = parse_cel_sort_spec("name asc")
    assert result == [("name", False)]


def test_parse_cel_sort_spec_with_desc() -> None:
    """parse_cel_sort_spec should parse descending direction."""
    result = parse_cel_sort_spec("name desc")
    assert result == [("name", True)]


def test_parse_cel_sort_spec_multiple_keys() -> None:
    """parse_cel_sort_spec should parse multiple comma-separated keys."""
    result = parse_cel_sort_spec("state, name asc, create_time desc")
    assert result == [("state", False), ("name", False), ("create_time", True)]


def test_parse_cel_sort_spec_nested_field() -> None:
    """parse_cel_sort_spec should handle nested fields like host.name."""
    result = parse_cel_sort_spec("host.name desc")
    assert result == [("host.name", True)]


def test_parse_cel_sort_spec_case_insensitive_direction() -> None:
    """parse_cel_sort_spec should handle case-insensitive directions."""
    result = parse_cel_sort_spec("name DESC")
    assert result == [("name", True)]


def test_parse_cel_sort_spec_ignores_empty_parts() -> None:
    """parse_cel_sort_spec should skip empty parts from trailing commas."""
    result = parse_cel_sort_spec("name,")
    assert result == [("name", False)]


# =============================================================================
# Tests for compile_cel_sort_keys
# =============================================================================


def test_compile_cel_sort_keys_valid_expression() -> None:
    """compile_cel_sort_keys should compile a valid CEL field expression."""
    compiled = compile_cel_sort_keys("name")
    assert len(compiled) == 1
    _program, is_descending = compiled[0]
    assert is_descending is False


def test_compile_cel_sort_keys_multiple_expressions() -> None:
    """compile_cel_sort_keys should compile multiple sort expressions."""
    compiled = compile_cel_sort_keys("name asc, create_time desc")
    assert len(compiled) == 2
    assert compiled[0][1] is False
    assert compiled[1][1] is True


def test_compile_cel_sort_keys_invalid_expression_raises() -> None:
    """compile_cel_sort_keys should raise MngError for invalid CEL syntax."""
    with pytest.raises(MngError, match="Invalid sort expression"):
        compile_cel_sort_keys("@#$invalid")


# =============================================================================
# Tests for evaluate_cel_sort_key
# =============================================================================


def test_evaluate_cel_sort_key_returns_value_for_existing_field() -> None:
    """evaluate_cel_sort_key should return the CEL value for a valid field."""
    compiled = compile_cel_sort_keys("name")
    program, _is_descending = compiled[0]
    cel_ctx = build_cel_context({"name": "test-agent"})
    result = evaluate_cel_sort_key(program, cel_ctx)
    assert str(result) == "test-agent"


def test_evaluate_cel_sort_key_returns_none_for_missing_field() -> None:
    """evaluate_cel_sort_key should return None when the field does not exist."""
    compiled = compile_cel_sort_keys("nonexistent")
    program, _is_descending = compiled[0]
    cel_ctx = build_cel_context({"name": "test-agent"})
    result = evaluate_cel_sort_key(program, cel_ctx)
    assert result is None
