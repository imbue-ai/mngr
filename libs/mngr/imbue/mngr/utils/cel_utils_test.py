"""Tests for CEL utilities."""

import celpy
import celpy.celtypes
import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr.utils.cel_utils import TolerantMapType
from imbue.mngr.utils.cel_utils import apply_cel_filters_to_context
from imbue.mngr.utils.cel_utils import build_cel_context
from imbue.mngr.utils.cel_utils import compile_cel_filters
from imbue.mngr.utils.cel_utils import compile_cel_sort_keys
from imbue.mngr.utils.cel_utils import evaluate_cel_sort_key
from imbue.mngr.utils.cel_utils import parse_cel_sort_spec
from imbue.mngr.utils.cel_utils import tolerant_dict
from imbue.mngr.utils.testing import capture_loguru


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


def test_cel_invalid_include_filter_raises_mngr_error() -> None:
    """compile_cel_filters should raise MngrError for invalid include filter."""
    with pytest.raises(MngrError, match="Invalid include filter"):
        compile_cel_filters(
            include_filters=("this is not valid cel @@@@",),
            exclude_filters=(),
        )


def test_cel_invalid_exclude_filter_raises_mngr_error() -> None:
    """compile_cel_filters should raise MngrError for invalid exclude filter."""
    with pytest.raises(MngrError, match="Invalid exclude filter"):
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
    """compile_cel_sort_keys should raise MngrError for invalid CEL syntax."""
    with pytest.raises(MngrError, match="Invalid sort expression"):
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


# =============================================================================
# Tests for tolerant_dict / TolerantMapType
# =============================================================================


def test_tolerant_map_missing_key_does_not_warn_in_exclude() -> None:
    """Excluding by labels.X on an empty tolerant labels dict must not warn."""
    includes, excludes = compile_cel_filters(
        include_filters=(),
        exclude_filters=('labels.mngr_subagent_proxy == "child"',),
    )
    with capture_loguru(level="WARNING") as log_output:
        result = apply_cel_filters_to_context(
            context={"labels": tolerant_dict({})},
            include_filters=includes,
            exclude_filters=excludes,
            error_context_description="agent test",
        )
    assert result is True
    assert "Error evaluating" not in log_output.getvalue()


def test_tolerant_map_missing_key_in_include_filter_evaluates_false() -> None:
    """Including by labels.X on an empty tolerant labels dict evaluates False without warning."""
    includes, excludes = compile_cel_filters(
        include_filters=('labels.project == "mngr"',),
        exclude_filters=(),
    )
    with capture_loguru(level="WARNING") as log_output:
        result = apply_cel_filters_to_context(
            context={"labels": tolerant_dict({})},
            include_filters=includes,
            exclude_filters=excludes,
            error_context_description="agent test",
        )
    assert result is False
    assert "Error evaluating" not in log_output.getvalue()


def test_tolerant_map_present_key_compares_normally() -> None:
    """Tolerant maps still compare normally when the key is present."""
    includes, excludes = compile_cel_filters(
        include_filters=('labels.project == "mngr"',),
        exclude_filters=(),
    )
    result = apply_cel_filters_to_context(
        context={"labels": tolerant_dict({"project": "mngr"})},
        include_filters=includes,
        exclude_filters=excludes,
        error_context_description="agent test",
    )
    assert result is True


def test_strict_map_type_raises_on_missing_key() -> None:
    """Plain MapType still raises KeyError on missing-key access (regression guard).

    This is what the strict-warning code path hangs on: the per-agent warning
    is logged when a strict map raises and the filter eval surfaces a
    CELEvalError. The tolerant subclass deliberately diverges from this for
    schemaless fields; the strict default must remain.
    """
    strict = celpy.celtypes.MapType({celpy.json_to_cel("present"): celpy.json_to_cel("v")})
    assert strict[celpy.json_to_cel("present")] == celpy.json_to_cel("v")
    with pytest.raises(KeyError):
        _ = strict[celpy.json_to_cel("missing")]


def test_tolerant_map_type_returns_none_on_missing_key() -> None:
    """TolerantMapType returns None on missing-key access (the core change)."""
    tolerant = TolerantMapType({celpy.json_to_cel("present"): celpy.json_to_cel("v")})
    assert tolerant[celpy.json_to_cel("present")] == celpy.json_to_cel("v")
    assert tolerant[celpy.json_to_cel("missing")] is None


def test_tolerant_map_has_macro_always_true() -> None:
    """has() on a TolerantMapType always returns True.

    Trade-off documented on TolerantMapType: cel-python's has() macro reports
    "is this expression non-erroring?", so a tolerant lookup that returns None
    instead of raising is treated as present. Use `field != null` or direct
    comparison to test for presence on tolerant fields.
    """
    includes, excludes = compile_cel_filters(
        include_filters=("has(labels.archived_at)",),
        exclude_filters=(),
    )
    result_missing = apply_cel_filters_to_context(
        context={"labels": tolerant_dict({})},
        include_filters=includes,
        exclude_filters=excludes,
        error_context_description="agent test",
    )
    result_present = apply_cel_filters_to_context(
        context={"labels": tolerant_dict({"archived_at": "2024-01-01"})},
        include_filters=includes,
        exclude_filters=excludes,
        error_context_description="agent test",
    )
    assert result_missing is True
    assert result_present is True


def test_tolerant_map_nested_dict_inner_missing_key() -> None:
    """A nested tolerant dict inside another tolerant dict swallows missing keys cleanly."""
    includes, excludes = compile_cel_filters(
        include_filters=(),
        exclude_filters=('plugin.foo.bar == "x"',),
    )
    with capture_loguru(level="WARNING") as log_output:
        result = apply_cel_filters_to_context(
            context={"plugin": tolerant_dict({"foo": tolerant_dict({})})},
            include_filters=includes,
            exclude_filters=excludes,
            error_context_description="agent test",
        )
    assert result is True
    assert "Error evaluating" not in log_output.getvalue()


def test_tolerant_marker_at_depth_in_strict_dict() -> None:
    """Marking a nested dict only relaxes that level; siblings stay strict.

    The build_cel_context output is inspected directly because cel-python's
    behavior on missing-key access has shifted between versions (0.4.0 returns
    BoolType(False) silently; 0.5.0 raises CELEvalError that the filter loop
    reports as a warning). Asserting on the runtime types makes this test
    independent of celpy version while still proving the marker is honored
    only at the wrapped level.
    """
    raw_context: dict = {"host": {"tags": tolerant_dict({}), "name": "h1"}}
    cel_context = build_cel_context(raw_context)
    host = cel_context["host"]
    assert isinstance(host, celpy.celtypes.MapType)
    assert not isinstance(host, TolerantMapType)
    tags = host[celpy.json_to_cel("tags")]
    assert isinstance(tags, TolerantMapType)
    assert tags[celpy.json_to_cel("foo")] is None
    with pytest.raises(KeyError):
        _ = host[celpy.json_to_cel("providr")]
