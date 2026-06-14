"""Unit tests for the core merge algebra: apply_extend/extend_dict, combine_patches,
merge/finalize, and the narrowing predicate."""

from typing import Any

import pytest

from imbue.overlay.errors import OverlayError
from imbue.overlay.markers import ScalarTuple
from imbue.overlay.markers import StaticDict
from imbue.overlay.markers import StaticList
from imbue.overlay.markers import StaticTuple
from imbue.overlay.merge import apply_extend
from imbue.overlay.merge import combine_patches
from imbue.overlay.merge import extend_dict
from imbue.overlay.merge import finalize
from imbue.overlay.merge import merge
from imbue.overlay.merge import would_assignment_narrow
from imbue.overlay.operators import is_extend_key

# =============================================================================
# apply_extend / extend_dict -- leaf and recursive resolution
# =============================================================================


def test_apply_extend_concats_lists() -> None:
    assert apply_extend(["A"], ["B"], "f") == ["A", "B"]


def test_apply_extend_concats_tuples_preserving_type() -> None:
    assert apply_extend(("A",), ("B",), "f") == ("A", "B")
    assert isinstance(apply_extend(("A",), ("B",), "f"), tuple)


def test_apply_extend_unions_sets() -> None:
    assert apply_extend({"A"}, ["B"], "f") == {"A", "B"}


def test_apply_extend_unions_frozensets() -> None:
    result = apply_extend(frozenset({"A"}), ["B"], "f")
    assert result == frozenset({"A", "B"})
    assert isinstance(result, frozenset)


def test_apply_extend_recurses_into_dict() -> None:
    base = {"defaultMode": "acceptEdits", "allow": ["old"]}
    extend = {"allow__extend": ["new"]}
    assert apply_extend(base, extend, "permissions") == {"defaultMode": "acceptEdits", "allow": ["old", "new"]}


def test_apply_extend_against_none_assigns_aggregate() -> None:
    assert apply_extend(None, ["A"], "f") == ["A"]


def test_apply_extend_against_none_resolves_nested_markers() -> None:
    assert apply_extend(None, {"allow__extend": ["X"]}, "f") == {"allow": ["X"]}


def test_apply_extend_against_none_rejects_scalar() -> None:
    with pytest.raises(OverlayError, match="requires a list, tuple, dict, or set value"):
        apply_extend(None, "scalar", "f")


def test_apply_extend_list_rejects_non_array() -> None:
    with pytest.raises(OverlayError, match="requires a JSON array value"):
        apply_extend(["A"], {"not": "array"}, "f")


def test_apply_extend_set_rejects_non_array() -> None:
    with pytest.raises(OverlayError, match="requires a JSON array value"):
        apply_extend({"A"}, "scalar", "f")


def test_apply_extend_dict_rejects_non_object() -> None:
    with pytest.raises(OverlayError, match="requires a JSON object value"):
        apply_extend({"a": 1}, ["not", "a", "dict"], "f")


def test_apply_extend_rejects_extend_on_scalar() -> None:
    with pytest.raises(OverlayError, match="target field is a scalar"):
        apply_extend("base", "oops", "f")


def test_extend_dict_assigns_before_extending_in_same_layer() -> None:
    """Bare keys (assign-phase) apply before sibling ``__extend`` (extend-phase)."""
    result = extend_dict({"f": ["BASE"]}, {"f": [], "f__extend": ["A"]}, "")
    assert result == {"f": ["A"]}


def test_extend_dict_nested_bare_key_replaces_preserving_siblings() -> None:
    base = {"a": {"x": 1, "y": 2}}
    result = extend_dict(base, {"a": {"x": 9}}, "")
    assert result == {"a": {"x": 9}}


def test_extend_dict_raises_on_conflicting_assign() -> None:
    with pytest.raises(OverlayError, match="Conflicting assignment"):
        extend_dict({}, {"f": 1, "f__assign": 2}, "")


# =============================================================================
# combine_patches -- cross-layer patch combine (four-rule table)
# =============================================================================


def test_combine_patches_extend_plus_extend_accumulates_marker() -> None:
    """Row 1: ``f__extend=A`` (lower) + ``f__extend=B`` (higher) -> ``f__extend=A+B``."""
    combined = combine_patches({"f__extend": ["A"]}, {"f__extend": ["B"]})
    assert combined == {"f__extend": ["A", "B"]}


def test_combine_patches_lower_bare_plus_higher_extend_stays_bare() -> None:
    """Row 2: ``f=A`` (lower bare) + ``f__extend=B`` (higher) -> bare ``f=A+B``."""
    combined = combine_patches({"f": ["A"]}, {"f__extend": ["B"]})
    assert combined == {"f": ["A", "B"]}


def test_combine_patches_higher_bare_wipes_lower_extend() -> None:
    """Row 3: ``f__extend=A`` (lower) + ``f=B`` (higher bare) -> bare ``f=B``."""
    combined = combine_patches({"f__extend": ["A"]}, {"f": ["B"]})
    assert combined == {"f": ["B"]}


def test_combine_patches_higher_bare_wipes_lower_bare() -> None:
    """Row 4: ``f=A`` (lower bare) + ``f=B`` (higher bare) -> bare ``f=B``."""
    combined = combine_patches({"f": ["A"]}, {"f": ["B"]})
    assert combined == {"f": ["B"]}


def test_combine_patches_lower_only_keys_carry_through() -> None:
    """Keys present only in ``lower`` are preserved unchanged (bare and marker)."""
    combined = combine_patches({"a": 1, "b__extend": ["x"]}, {"c": 2})
    assert combined == {"a": 1, "b__extend": ["x"], "c": 2}


def test_combine_patches_extend_plus_extend_recurses_into_nested_dict() -> None:
    """Dict-valued markers combine recursively, preserving nested markers."""
    lower = {"permissions__extend": {"allow__extend": ["X"]}}
    higher = {"permissions__extend": {"allow__extend": ["Y"], "deny__extend": ["Z"]}}
    combined = combine_patches(lower, higher)
    assert combined == {"permissions__extend": {"allow__extend": ["X", "Y"], "deny__extend": ["Z"]}}


def test_combine_patches_higher_bare_dict_strips_lower_contribution() -> None:
    """A higher *bare* dict value wins and does not merge in the lower marker; its
    own nested markers are kept structured (combined against nothing)."""
    lower = {"permissions__extend": {"allow__extend": ["X"]}}
    higher = {"permissions": {"deny__extend": ["Z"]}}
    combined = combine_patches(lower, higher)
    assert combined == {"permissions": {"deny__extend": ["Z"]}}


def test_combine_patches_lower_bare_dict_with_nested_marker_plus_higher_extend() -> None:
    """Row 2, dict case: a lower *bare* dict carrying a nested marker, combined with a
    higher dict marker, interleaves the nested markers (lower then higher) in the bare
    slot so a later fold extends in the correct precedence order."""
    lower = {"permissions": {"defaultMode": "acceptEdits", "allow__extend": ["X"]}}
    higher = {"permissions__extend": {"allow__extend": ["Y"]}}
    combined = combine_patches(lower, higher)
    assert combined == {"permissions": {"defaultMode": "acceptEdits", "allow__extend": ["X", "Y"]}}


def test_combine_patches_higher_assign_wins_and_keeps_suffix() -> None:
    """A higher ``__assign`` wins over a lower marker (like bare) and keeps its suffix
    so the eventual fold suppresses narrowing."""
    combined = combine_patches({"f__extend": ["A"]}, {"f__assign": ["B"]})
    assert combined == {"f__assign": ["B"]}


def test_combine_patches_lower_assign_plus_higher_extend_keeps_assign_suffix() -> None:
    """A lower ``__assign`` extended by a higher ``__extend`` extends the assigned
    value and retains the ``__assign`` suffix (no-warn intent preserved)."""
    combined = combine_patches({"f__assign": ["A"]}, {"f__extend": ["B"]})
    assert combined == {"f__assign": ["A", "B"]}


def test_combine_patches_bare_plus_assign_same_layer_raises() -> None:
    with pytest.raises(OverlayError, match="Conflicting assignment"):
        combine_patches({}, {"f": 1, "f__assign": 2})


def test_combine_patches_combines_set_markers() -> None:
    combined = combine_patches({"f__extend": {"A"}}, {"f__extend": ["B"]})
    assert combined == {"f__extend": {"A", "B"}}


def test_combine_patches_incompatible_marker_shapes_raise() -> None:
    with pytest.raises(OverlayError, match="incompatible shapes"):
        combine_patches({"f__extend": ["A"]}, {"f__extend": {"k": "v"}})


# =============================================================================
# merge / finalize -- associativity and threaded narrowing
# =============================================================================


def _fold(base: dict[str, Any], patch: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """``merge`` against the concrete ``base`` (tracking narrowings) then ``finalize``."""
    merged, narrowings = merge(base, patch)
    return finalize(merged), narrowings


@pytest.mark.parametrize(
    ("base", "lower", "higher"),
    [
        ({"f": ["V"]}, {"f__extend": ["A"]}, {"f__extend": ["B"]}),
        ({"f": ["V"]}, {"f": ["A"]}, {"f__extend": ["B"]}),
        ({"f": ["V"]}, {"f__extend": ["A"]}, {"f": ["B"]}),
        ({"f": ["V"]}, {"f": ["A"]}, {"f": ["B"]}),
        (
            {"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}},
            {"permissions__extend": {"allow__extend": ["X"]}},
            {"permissions__extend": {"allow__extend": ["Y"]}},
        ),
        ({"p": {"k": 1}}, {"a__extend": ["x"]}, {"b__extend": ["y"]}),
        (
            {"permissions": {"allow": ["base"]}},
            {"permissions": {"defaultMode": "acceptEdits", "allow__extend": ["git"]}},
            {"permissions__extend": {"allow__extend": ["npm"]}},
        ),
        (
            {"a": "base"},
            {"a": {"c__extend": {"b": "lower"}}},
            {"a__extend": {"c__extend": {"b": "higher"}}},
        ),
        ({"f": ["V"]}, {"f__extend": ["A"]}, {"f__assign": ["B"]}),
    ],
)
def test_merge_is_associative_under_finalize(
    base: dict[str, Any], lower: dict[str, Any], higher: dict[str, Any]
) -> None:
    """``finalize(merge(merge(B, X), Y)) == finalize(merge(B, merge(X, Y)))`` for the
    four-rule table plus nested-dict recursion and ``__assign``."""
    left = finalize(merge(merge(base, lower)[0], higher)[0])
    right = finalize(merge(base, merge(lower, higher)[0])[0])
    assert left == right


def test_merge_extend_against_present_dict_preserves_siblings() -> None:
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}}
    merged, narrowings = _fold(base, {"permissions__extend": {"allow__extend": ["X"]}})
    assert merged == {"permissions": {"defaultMode": "acceptEdits", "allow": ["old", "X"]}}
    assert narrowings == []


def test_merge_top_level_bare_narrows() -> None:
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits"}}
    merged, narrowings = _fold(base, {"permissions": {"allow": ["X"]}})
    assert merged == {"permissions": {"allow": ["X"]}}
    assert narrowings == ["permissions"]


def test_merge_nested_bare_inside_extend_narrows() -> None:
    """A bare key nested inside an ``__extend`` value that drops a non-empty base
    aggregate is recorded at its dotted path (the recursive-narrowing case)."""
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}}
    merged, narrowings = _fold(base, {"permissions__extend": {"allow": ["X"]}})
    assert merged == {"permissions": {"defaultMode": "acceptEdits", "allow": ["X"]}}
    assert narrowings == ["permissions.allow"]


def test_merge_assigns_absent_key_without_narrowing() -> None:
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits"}}
    merged, narrowings = _fold(base, {"model": "opus"})
    assert merged == {"permissions": {"defaultMode": "acceptEdits"}, "model": "opus"}
    assert narrowings == []


def test_merge_resolves_nested_markers_in_bare_dict() -> None:
    base: dict[str, Any] = {}
    merged, narrowings = _fold(base, {"permissions": {"allow__extend": ["X"]}})
    assert merged == {"permissions": {"allow": ["X"]}}
    assert narrowings == []


def test_finalize_output_has_no_markers() -> None:
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}}
    merged, _ = _fold(base, {"permissions__extend": {"allow__extend": ["X"]}})

    def _has_marker(value: Any) -> bool:
        if isinstance(value, dict):
            return any(is_extend_key(k) for k in value) or any(_has_marker(v) for v in value.values())
        if isinstance(value, list):
            return any(_has_marker(v) for v in value)
        return False

    assert not _has_marker(merged)


def test_merge_against_concrete_base_preserves_untouched_siblings() -> None:
    base: dict[str, Any] = {"model": "opus", "permissions": {"allow": ["old"]}}
    merged, narrowings = _fold(base, {"permissions__extend": {"allow__extend": ["X"]}})
    assert merged == {"model": "opus", "permissions": {"allow": ["old", "X"]}}
    assert narrowings == []


def test_merge_static_override_does_not_narrow() -> None:
    base: dict[str, Any] = {"cli_args": ["--debug", "--trace"]}
    merged, narrowings = _fold(base, {"cli_args": StaticList(["--verbose"])})
    assert merged == {"cli_args": ["--verbose"]}
    assert narrowings == []


def test_merge_assign_suppresses_narrowing_but_bare_does_not() -> None:
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}}
    _, bare_narrowings = _fold(base, {"permissions": {"allow": ["X"]}})
    assert bare_narrowings == ["permissions"]
    _, assign_narrowings = _fold(base, {"permissions__assign": {"allow": ["X"]}})
    assert assign_narrowings == []


def test_fold_assign_then_extend_in_same_layer_resets_without_warning_then_adds() -> None:
    base: dict[str, Any] = {"unset_vars": ["OLD"]}
    merged, narrowings = _fold(base, {"unset_vars__assign": [], "unset_vars__extend": ["A"]})
    assert merged == {"unset_vars": ["A"]}
    assert narrowings == []


def test_fold_assign_nested_inside_extend_suppresses_nested_narrowing() -> None:
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}}
    merged, narrowings = _fold(base, {"permissions__extend": {"allow__assign": ["X"]}})
    assert merged == {"permissions": {"defaultMode": "acceptEdits", "allow": ["X"]}}
    assert narrowings == []


def test_fold_bare_plus_assign_same_key_raises() -> None:
    with pytest.raises(OverlayError, match="Conflicting assignment"):
        _fold({}, {"model": "opus", "model__assign": "sonnet"})


# =============================================================================
# would_assignment_narrow -- value-level narrowing predicate
# =============================================================================


def test_would_assignment_narrow_flags_list_drop() -> None:
    assert would_assignment_narrow(["a", "b"], ["c"]) is True


def test_would_assignment_narrow_allows_list_superset() -> None:
    assert would_assignment_narrow(["a"], ["a", "b"]) is False


def test_would_assignment_narrow_flags_empty_override_over_non_empty() -> None:
    assert would_assignment_narrow(["a"], []) is True


def test_would_assignment_narrow_ignores_empty_base() -> None:
    assert would_assignment_narrow([], ["a"]) is False


def test_would_assignment_narrow_ignores_scalar_base() -> None:
    assert would_assignment_narrow("x", "y") is False


def test_would_assignment_narrow_flags_set_drop() -> None:
    assert would_assignment_narrow({"a", "b"}, {"a"}) is True


def test_would_assignment_narrow_allows_set_superset() -> None:
    assert would_assignment_narrow({"a"}, {"a", "b"}) is False


def test_would_assignment_narrow_flags_dict_key_drop() -> None:
    assert would_assignment_narrow({"x": 1, "y": 2}, {"x": 1}) is True


def test_would_assignment_narrow_flags_non_dict_override_of_dict_base() -> None:
    assert would_assignment_narrow({"x": 1}, ["x"]) is True


def test_would_assignment_narrow_recurses_into_dict_values() -> None:
    assert would_assignment_narrow({"a": ["x", "y"]}, {"a": ["x"]}) is True
    assert would_assignment_narrow({"a": ["x"]}, {"a": ["x", "y"]}) is False


def test_would_assignment_narrow_exempts_static_list() -> None:
    assert would_assignment_narrow(["a", "b"], StaticList(["c"])) is False
    assert would_assignment_narrow(["a", "b"], ["c"]) is True


def test_would_assignment_narrow_exempts_static_dict() -> None:
    assert would_assignment_narrow({"x": 1, "y": 2}, StaticDict({"x": 1})) is False
    assert would_assignment_narrow({"x": 1, "y": 2}, {"x": 1}) is True


def test_would_assignment_narrow_exempts_static_tuple() -> None:
    assert would_assignment_narrow(("a", "b"), StaticTuple(("c",))) is False
    assert would_assignment_narrow(("a", "b"), ("c",)) is True


def test_would_assignment_narrow_exempts_scalar_tuple() -> None:
    assert would_assignment_narrow(("0.0.0.0/0",), ScalarTuple(("203.0.113.4/32",))) is False
    assert would_assignment_narrow(("0.0.0.0/0",), ("203.0.113.4/32",)) is True
