"""Tests for CEL utilities."""

from typing import Any

import celpy
import celpy.celtypes
import pytest
from celpy.evaluation import CELEvalError

from imbue.mngr.errors import MngrError
from imbue.mngr.utils.cel_utils import TolerantMapType
from imbue.mngr.utils.cel_utils import TolerantPathError
from imbue.mngr.utils.cel_utils import apply_cel_filters_to_context
from imbue.mngr.utils.cel_utils import apply_compiled_cel_filters
from imbue.mngr.utils.cel_utils import build_cel_context
from imbue.mngr.utils.cel_utils import compile_cel_filters
from imbue.mngr.utils.cel_utils import compile_cel_sort_keys
from imbue.mngr.utils.cel_utils import evaluate_cel_sort_key
from imbue.mngr.utils.cel_utils import parse_cel_sort_spec
from imbue.mngr.utils.cel_utils import with_tolerant_paths
from imbue.mngr.utils.testing import capture_loguru


def _build_cel_context_with_tolerant_paths(
    raw_context: dict[str, Any], paths: tuple[tuple[str, ...], ...]
) -> dict[str, Any]:
    """Test helper: convert + apply tolerance, mirroring how production callers compose."""
    return with_tolerant_paths(build_cel_context(raw_context), paths)


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


@pytest.mark.allow_warnings
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


@pytest.mark.allow_warnings
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
    """build_cel_context should convert raw string values into CEL StringType values.

    The rest of the module relies on the values being real celpy.celtypes, so a
    regression that left them as plain Python str (or returned the raw dict) must fail.
    """
    raw = {"name": "test-agent", "state": "running"}
    cel_ctx = build_cel_context(raw)
    assert isinstance(cel_ctx["name"], celpy.celtypes.StringType)
    assert isinstance(cel_ctx["state"], celpy.celtypes.StringType)
    assert cel_ctx["name"] == celpy.celtypes.StringType("test-agent")
    assert cel_ctx["state"] == celpy.celtypes.StringType("running")


def test_build_cel_context_converts_nested_dicts() -> None:
    """build_cel_context should convert nested dicts to MapType so dot notation works.

    A nested dict must become a celpy MapType whose values are themselves CEL types,
    otherwise dot-notation filtering against the nested fields would break.
    """
    raw = {"host": {"provider": "local", "name": "my-host"}}
    cel_ctx = build_cel_context(raw)
    host = cel_ctx["host"]
    assert isinstance(host, celpy.celtypes.MapType)
    assert isinstance(host[celpy.json_to_cel("provider")], celpy.celtypes.StringType)
    assert host[celpy.json_to_cel("provider")] == celpy.celtypes.StringType("local")
    assert host[celpy.json_to_cel("name")] == celpy.celtypes.StringType("my-host")


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
# Tests for TolerantMapType / with_tolerant_paths
# =============================================================================


def test_tolerant_map_missing_key_does_not_warn_in_exclude() -> None:
    """Excluding by labels.X on an empty tolerant labels dict must not warn."""
    includes, excludes = compile_cel_filters(
        include_filters=(),
        exclude_filters=('labels.mngr_subagent_proxy == "child"',),
    )
    cel_context = _build_cel_context_with_tolerant_paths({"labels": {}}, (("labels",),))
    with capture_loguru(level="WARNING") as log_output:
        result = apply_compiled_cel_filters(
            cel_context=cel_context,
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
    cel_context = _build_cel_context_with_tolerant_paths({"labels": {}}, (("labels",),))
    with capture_loguru(level="WARNING") as log_output:
        result = apply_compiled_cel_filters(
            cel_context=cel_context,
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
    cel_context = _build_cel_context_with_tolerant_paths({"labels": {"project": "mngr"}}, (("labels",),))
    result = apply_compiled_cel_filters(
        cel_context=cel_context,
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


def test_tolerant_map_type_returns_cel_eval_error_on_missing_key() -> None:
    """TolerantMapType yields a CELEvalError value (not raises) on missing keys.

    Returning a CELEvalError lets cel-python's evaluator carry the error
    through boolean ops (so `labels.X == "Y"` short-circuits to False) and
    lets the `has()` macro correctly detect absence.
    """
    tolerant = TolerantMapType({celpy.json_to_cel("present"): celpy.json_to_cel("v")})
    assert tolerant[celpy.json_to_cel("present")] == celpy.json_to_cel("v")
    assert isinstance(tolerant[celpy.json_to_cel("missing")], CELEvalError)


def test_tolerant_map_has_macro_correctly_reports_presence() -> None:
    """has() on a TolerantMapType correctly distinguishes present vs missing keys."""
    includes_has, excludes_has = compile_cel_filters(
        include_filters=("has(labels.archived_at)",),
        exclude_filters=(),
    )
    cel_context_missing = _build_cel_context_with_tolerant_paths({"labels": {}}, (("labels",),))
    cel_context_present = _build_cel_context_with_tolerant_paths(
        {"labels": {"archived_at": "2024-01-01"}}, (("labels",),)
    )
    result_missing = apply_compiled_cel_filters(
        cel_context=cel_context_missing,
        include_filters=includes_has,
        exclude_filters=excludes_has,
        error_context_description="agent test",
    )
    result_present = apply_compiled_cel_filters(
        cel_context=cel_context_present,
        include_filters=includes_has,
        exclude_filters=excludes_has,
        error_context_description="agent test",
    )
    assert result_missing is False
    assert result_present is True


def test_with_tolerant_paths_at_nested_path() -> None:
    """A nested path target gets wrapped tolerantly; siblings stay strict."""
    raw_context = {"host": {"tags": {}, "name": "h1"}}
    original = build_cel_context(raw_context)
    new_context = with_tolerant_paths(original, (("host", "tags"),))
    host = new_context["host"]
    assert isinstance(host, celpy.celtypes.MapType)
    assert not isinstance(host, TolerantMapType)
    tags = host[celpy.json_to_cel("tags")]
    assert isinstance(tags, TolerantMapType)
    assert isinstance(tags[celpy.json_to_cel("foo")], CELEvalError)
    with pytest.raises(KeyError):
        _ = host[celpy.json_to_cel("missing")]


def test_with_tolerant_paths_does_not_mutate_input() -> None:
    """The input cel_context is unchanged; tolerance is applied only on the returned copy."""
    raw_context = {"host": {"tags": {}, "name": "h1"}, "labels": {}}
    original = build_cel_context(raw_context)
    new_context = with_tolerant_paths(original, (("labels",), ("host", "tags")))

    # Top-level: original "labels" is still strict, new is tolerant.
    assert not isinstance(original["labels"], TolerantMapType)
    assert isinstance(new_context["labels"], TolerantMapType)
    # Nested: original host.tags is still strict, new host.tags is tolerant.
    original_tags = original["host"][celpy.json_to_cel("tags")]
    new_tags = new_context["host"][celpy.json_to_cel("tags")]
    assert not isinstance(original_tags, TolerantMapType)
    assert isinstance(new_tags, TolerantMapType)


def test_with_tolerant_paths_raises_tolerant_path_error_when_target_is_not_dict() -> None:
    """A precondition violation (path target is not a MapType) raises TolerantPathError.

    Misconfigured paths must surface immediately rather than silently no-op,
    so a path that names a non-dict target (e.g. a string field) is rejected
    loudly at setup time.
    """
    raw_context = {"name": "h1"}
    cel_context = build_cel_context(raw_context)
    with pytest.raises(TolerantPathError):
        _ = with_tolerant_paths(cel_context, (("name",),))


def test_with_tolerant_paths_raises_when_path_segment_missing() -> None:
    """A precondition violation (path's segment not present) raises TolerantPathError.

    Both "segment value is wrong type" and "segment is missing entirely"
    represent caller misconfiguration; both must fail loud so a typoed path
    name (e.g. "lables" instead of "labels") surfaces immediately rather
    than silently producing a no-op tolerance wrap.
    """
    raw_context: dict[str, Any] = {"labels": {}}
    cel_context = build_cel_context(raw_context)
    with pytest.raises(TolerantPathError):
        _ = with_tolerant_paths(cel_context, (("nonexistent",),))


def test_with_tolerant_paths_raises_when_nested_path_segment_missing() -> None:
    """A nested-path precondition violation also raises TolerantPathError loudly."""
    raw_context: dict[str, Any] = {"host": {"tags": {}}}
    cel_context = build_cel_context(raw_context)
    with pytest.raises(TolerantPathError):
        _ = with_tolerant_paths(cel_context, (("host", "missing_subfield"),))


def test_with_tolerant_paths_raises_on_empty_path() -> None:
    """An empty path tuple is a misconfiguration and must raise TolerantPathError.

    Without an explicit check the unpacking `*prefix, last = path` would raise
    a bare ValueError that doesn't tie back to the function or the caller's
    misconfiguration; this guards the documented loud-fail contract for that
    edge case.
    """
    raw_context: dict[str, Any] = {"labels": {}}
    cel_context = build_cel_context(raw_context)
    with pytest.raises(TolerantPathError):
        _ = with_tolerant_paths(cel_context, ((),))


def test_with_tolerant_paths_multiple_paths() -> None:
    """Multiple paths can be wrapped in one call; non-listed paths stay strict."""
    raw_context = {
        "labels": {"a": "1"},
        "plugin": {},
        "host": {"tags": {}, "name": "h1", "ssh": {"host": "h"}},
    }
    original = build_cel_context(raw_context)
    new_context = with_tolerant_paths(
        original,
        (("labels",), ("plugin",), ("host", "tags")),
    )
    assert isinstance(new_context["labels"], TolerantMapType)
    assert isinstance(new_context["plugin"], TolerantMapType)
    host = new_context["host"]
    assert isinstance(host, celpy.celtypes.MapType)
    assert not isinstance(host, TolerantMapType)
    assert isinstance(host[celpy.json_to_cel("tags")], TolerantMapType)
    # Non-listed nested maps (host.ssh) stay strict.
    ssh = host[celpy.json_to_cel("ssh")]
    assert isinstance(ssh, celpy.celtypes.MapType)
    assert not isinstance(ssh, TolerantMapType)
