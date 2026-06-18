"""Unit tests for the typed-node merge algebra: lift, combine, finalize, the public
``merge`` / ``merge_narrowing_allowed`` API, and the payload-level extend helpers."""

from typing import Any

import pytest

from imbue.overlay.errors import NarrowingError
from imbue.overlay.errors import OverlayError
from imbue.overlay.markers import ScalarTuple
from imbue.overlay.markers import StaticDict
from imbue.overlay.markers import StaticList
from imbue.overlay.node_merge import apply_extend
from imbue.overlay.node_merge import combine
from imbue.overlay.node_merge import combine_extend_payloads
from imbue.overlay.node_merge import extend_plain_value
from imbue.overlay.node_merge import finalize
from imbue.overlay.node_merge import finalize_payload
from imbue.overlay.node_merge import lift
from imbue.overlay.node_merge import lower
from imbue.overlay.node_merge import merge
from imbue.overlay.node_merge import merge_narrowing_allowed
from imbue.overlay.nodes import Assign
from imbue.overlay.nodes import Default
from imbue.overlay.nodes import Extend
from imbue.overlay.nodes import Patch
from imbue.overlay.nodes import is_assign_kind

# =============================================================================
# nodes -- predicates
# =============================================================================


def test_is_assign_kind_recognises_assign_kinds() -> None:
    assert is_assign_kind(Default([1]))
    assert is_assign_kind(Assign([1]))
    assert not is_assign_kind(Extend([1]))


# =============================================================================
# lift -- surface syntax -> nodes
# =============================================================================


def test_lift_bare_field_becomes_default() -> None:
    assert lift({"a": [1]}) == {"a": Default([1])}


def test_lift_assign_field_becomes_assign() -> None:
    assert lift({"a__assign": [1]}) == {"a": Assign([1])}


def test_lift_extend_field_becomes_extend() -> None:
    assert lift({"a__extend": [1]}) == {"a": Extend([1])}


def test_lift_bare_and_assign_same_field_raises() -> None:
    with pytest.raises(OverlayError, match="Conflicting assignment"):
        lift({"model": "opus", "model__assign": "sonnet"})


def test_lift_nested_dict_payload_recurses() -> None:
    assert lift({"p": {"allow__extend": ["X"]}}) == {"p": Default({"allow": Extend(["X"])})}


def test_lift_within_layer_bare_plus_extend_resets_then_adds() -> None:
    """A bare assign and an ``__extend`` for the same field fold into one ``Default``
    whose payload is the extend applied onto the assign payload."""
    assert lift({"f": ["BASE"], "f__extend": ["A"]}) == {"f": Default(["BASE", "A"])}


def test_lift_within_layer_assign_plus_extend_resets_then_adds() -> None:
    """``__assign`` + ``__extend`` folds into one ``Assign`` (reset-without-warning,
    then add)."""
    assert lift({"f__assign": [], "f__extend": ["A"]}) == {"f": Assign(["A"])}


def test_lift_within_layer_fold_is_order_independent() -> None:
    """The fold does not depend on which suffix key appears first in the source."""
    extend_first = lift({"f__extend": ["A"], "f": ["BASE"]})
    bare_first = lift({"f": ["BASE"], "f__extend": ["A"]})
    assert extend_first == bare_first == {"f": Default(["BASE", "A"])}


def test_lift_within_layer_fold_nested_dict() -> None:
    """The within-layer fold recurses for dict payloads (extend onto the assign)."""
    lifted = lift({"p": {"allow": ["old"]}, "p__extend": {"allow__extend": ["new"]}})
    assert lifted == {"p": Default({"allow": Default(["old", "new"])})}
    assert finalize(lifted) == {"p": {"allow": ["old", "new"]}}


# =============================================================================
# lift -- stacked-suffix regression (typed nodes make this safe by construction)
# =============================================================================


def test_lift_stacked_extend_assign_does_not_reactivate() -> None:
    """REGRESSION: ``a__extend__assign`` strips only the outermost ``__assign`` once,
    yielding the *literal* field name ``a__extend`` with a single ``Assign`` wrapper.

    Typed nodes are safe by construction: the algebra never re-parses a field name or
    unwraps a payload to look for an inner operator, so the inner ``__extend`` is inert
    forever -- ``finalize`` never fires an extend and never produces the key ``a``.
    """
    lifted = lift({"a__extend__assign": [1, 2]})
    assert lifted == {"a__extend": Assign([1, 2])}
    # Finalize keeps the literal key; the inner ``__extend`` never fires and ``a``
    # never appears. Finalize is over node patches and is idempotent on its own
    # output (a plain dict has no nodes), so re-finalizing the node patch is stable;
    # the suffix is never re-parsed because the algebra never re-lifts.
    once = finalize(lifted)
    assert once == {"a__extend": [1, 2]}
    assert "a" not in once
    # Merging the node patch against a base and finalizing again still never fires
    # an extend or produces ``a`` -- the wrapper is a plain ``Assign`` of a literal key.
    merged, narrowings = merge_narrowing_allowed({}, lifted)
    again = finalize(merged)
    assert again == {"a__extend": [1, 2]}
    assert "a" not in again
    assert narrowings == []


def test_lift_stacked_assign_extend_does_not_reactivate() -> None:
    """REGRESSION: ``a__assign__extend`` strips only the outer ``__extend`` once,
    yielding the literal field name ``a__assign`` with a single ``Extend`` wrapper;
    the inner ``__assign`` is inert and ``finalize`` never produces the key ``a``."""
    lifted = lift({"a__assign__extend": [1, 2]})
    assert lifted == {"a__assign": Extend([1, 2])}
    finalized = finalize(lifted)
    assert finalized == {"a__assign": [1, 2]}
    assert "a" not in finalized


# =============================================================================
# lower -- node Patch -> suffix-keyed surface dict (inverse of lift)
# =============================================================================


def test_lower_emits_suffix_per_node_kind() -> None:
    patch: Patch = {"a": Default(1), "b": Assign(2), "c": Extend([3])}
    assert lower(patch) == {"a": 1, "b__assign": 2, "c__extend": [3]}


def test_lower_recurses_into_nested_patch() -> None:
    patch: Patch = {"p": Extend({"allow": Default(["x"]), "deny": Extend(["y"])})}
    assert lower(patch) == {"p__extend": {"allow": ["x"], "deny__extend": ["y"]}}


def test_lift_lower_round_trips_a_combined_patch() -> None:
    """``lift(lower(patch))`` reproduces a combined patch, so a consumer can store an
    unresolved patch as suffix-keyed data and re-lift it later without drift."""
    source = {"keep": "v", "add__extend": ["x"], "force__assign": {"k": "v"}}
    patch = lift(source)
    assert lift(lower(patch)) == patch


def test_lower_then_relift_does_not_reactivate_stacked_suffix() -> None:
    """A stacked suffix lifts to a literal field name under a single wrapper; lowering
    re-emits that wrapper's own suffix, so re-lifting reproduces the same node rather
    than peeling the inner suffix into a second operator."""
    patch = lift({"a__extend__assign": [1, 2]})
    assert patch == {"a__extend": Assign([1, 2])}
    assert lower(patch) == {"a__extend__assign": [1, 2]}
    assert lift(lower(patch)) == patch


# =============================================================================
# apply_extend / combine_extend_payloads -- payload-level extend
# =============================================================================


def test_apply_extend_concats_lists() -> None:
    assert apply_extend(["A"], ["B"], "f") == ["A", "B"]


def test_apply_extend_concats_tuples_preserving_type() -> None:
    result = apply_extend(("A",), ("B",), "f")
    assert result == ("A", "B")
    assert isinstance(result, tuple)


def test_apply_extend_unions_sets() -> None:
    assert apply_extend({"A"}, ["B"], "f") == {"A", "B"}


def test_apply_extend_unions_frozensets() -> None:
    result = apply_extend(frozenset({"A"}), ["B"], "f")
    assert result == frozenset({"A", "B"})
    assert isinstance(result, frozenset)


def test_apply_extend_recurses_into_patch() -> None:
    base = {"defaultMode": Default("acceptEdits"), "allow": Default(["old"])}
    extend = {"allow": Extend(["new"])}
    result = apply_extend(base, extend, "permissions")
    assert finalize(result) == {"defaultMode": "acceptEdits", "allow": ["old", "new"]}


def test_apply_extend_against_none_assigns_aggregate() -> None:
    assert apply_extend(None, ["A"], "f") == ["A"]


def test_apply_extend_against_none_resolves_nested_patch() -> None:
    result = apply_extend(None, {"allow": Extend(["X"])}, "f")
    assert finalize(result) == {"allow": ["X"]}


def test_apply_extend_against_none_rejects_scalar() -> None:
    with pytest.raises(OverlayError, match="requires a list, tuple, dict, or set value"):
        apply_extend(None, "scalar", "f")


def test_apply_extend_list_rejects_non_array() -> None:
    with pytest.raises(OverlayError, match="requires a JSON array value"):
        apply_extend(["A"], "scalar", "f")


def test_apply_extend_set_rejects_non_array() -> None:
    with pytest.raises(OverlayError, match="requires a JSON array value"):
        apply_extend({"A"}, "scalar", "f")


def test_apply_extend_dict_rejects_non_object() -> None:
    with pytest.raises(OverlayError, match="requires a JSON object value"):
        apply_extend({"a": Default(1)}, ["not", "a", "dict"], "f")


def test_apply_extend_rejects_extend_on_scalar() -> None:
    with pytest.raises(OverlayError, match="target field is a scalar"):
        apply_extend("base", "oops", "f")


def test_combine_extend_payloads_concats_lists() -> None:
    assert combine_extend_payloads(["A"], ["B"], "f") == ["A", "B"]


def test_combine_extend_payloads_concats_tuples_preserving_type() -> None:
    result = combine_extend_payloads(("A",), ("B",), "f")
    assert result == ("A", "B")
    assert isinstance(result, tuple)


def test_combine_extend_payloads_unions_sets() -> None:
    assert combine_extend_payloads({"A"}, ["B"], "f") == {"A", "B"}


def test_combine_extend_payloads_unions_frozensets() -> None:
    result = combine_extend_payloads(frozenset({"A"}), ["B"], "f")
    assert result == frozenset({"A", "B"})
    assert isinstance(result, frozenset)


def test_combine_extend_payloads_recurses_into_patch() -> None:
    lower = {"allow": Extend(["X"])}
    higher = {"allow": Extend(["Y"]), "deny": Extend(["Z"])}
    result = combine_extend_payloads(lower, higher, "permissions")
    assert result == {"allow": Extend(["X", "Y"]), "deny": Extend(["Z"])}


def test_combine_extend_payloads_incompatible_shapes_raise() -> None:
    with pytest.raises(OverlayError, match="incompatible shapes"):
        combine_extend_payloads(["A"], {"k": Default("v")}, "f")


# =============================================================================
# combine / combine_nodes -- cross-layer combine
# =============================================================================


def test_combine_assign_kind_wins_wholesale() -> None:
    """A higher assign-kind node replaces the lower node entirely."""
    combined = combine({"f": Default(["A"])}, {"f": Default(["B"])})
    assert combined == {"f": Default(["B"])}


def test_combine_higher_assign_wins_over_lower_extend() -> None:
    combined = combine({"f": Extend(["A"])}, {"f": Assign(["B"])})
    assert combined == {"f": Assign(["B"])}


def test_combine_extend_over_default_stays_default() -> None:
    """``Extend`` over ``Default`` keeps the lower's kind, extending its payload."""
    combined = combine({"f": Default(["A"])}, {"f": Extend(["B"])})
    assert combined == {"f": Default(["A", "B"])}


def test_combine_extend_over_assign_stays_assign() -> None:
    combined = combine({"f": Assign(["A"])}, {"f": Extend(["B"])})
    assert combined == {"f": Assign(["A", "B"])}


def test_combine_extend_over_extend_stays_extend_concat() -> None:
    combined = combine({"f": Extend(["A"])}, {"f": Extend(["B"])})
    assert combined == {"f": Extend(["A", "B"])}


def test_combine_extend_over_extend_recurses_into_patch() -> None:
    lower: Patch = {"p": Extend({"allow": Extend(["X"])})}
    higher: Patch = {"p": Extend({"allow": Extend(["Y"]), "deny": Extend(["Z"])})}
    combined = combine(lower, higher)
    assert combined == {"p": Extend({"allow": Extend(["X", "Y"]), "deny": Extend(["Z"])})}


def test_combine_keys_in_one_side_carry_through() -> None:
    combined = combine({"a": Default(1), "b": Extend(["x"])}, {"c": Default(2)})
    assert combined == {"a": Default(1), "b": Extend(["x"]), "c": Default(2)}


def test_combine_default_over_extend_records_no_narrowing() -> None:
    """A ``Default`` replacing a lower ``Extend`` records no narrowing (the lower
    increment is not a base to narrow)."""
    _, narrowings = merge_narrowing_allowed({"f": Extend(["A", "B"])}, {"f": Default(["C"])})
    assert narrowings == []


# =============================================================================
# finalize / finalize_payload -- collapse to plain values
# =============================================================================


def test_finalize_collapses_each_node_kind() -> None:
    patch: Patch = {"a": Default(1), "b": Assign([2]), "c": Extend(["x"])}
    assert finalize(patch) == {"a": 1, "b": [2], "c": ["x"]}


def test_finalize_recurses_into_nested_patch() -> None:
    patch: Patch = {"p": Default({"allow": Extend(["X"])})}
    assert finalize(patch) == {"p": {"allow": ["X"]}}


def test_finalize_surviving_extend_collapses_to_assign() -> None:
    """A surviving ``Extend`` (nothing to extend against) collapses to its payload."""
    assert finalize({"f": Extend(["X"])}) == {"f": ["X"]}


def test_finalize_payload_returns_leaf_unchanged() -> None:
    assert finalize_payload(["a", "b"]) == ["a", "b"]


# =============================================================================
# merge / merge_narrowing_allowed -- public API and narrowing
# =============================================================================


def test_merge_returns_patch_when_no_narrowing() -> None:
    base = lift({"permissions": {"allow": ["old"]}})
    higher = lift({"permissions__extend": {"allow__extend": ["X"]}})
    merged = merge(base, higher)
    assert finalize(merged) == {"permissions": {"allow": ["old", "X"]}}


def test_merge_raises_aggregating_all_narrowing_paths() -> None:
    base = lift({"a": ["x"], "b": ["y"]})
    higher = lift({"a": [], "b": []})
    with pytest.raises(NarrowingError) as exc_info:
        merge(base, higher)
    assert sorted(exc_info.value.paths) == ["a", "b"]
    assert "a" in str(exc_info.value) and "b" in str(exc_info.value)


def test_merge_narrowing_allowed_returns_paths_without_raising() -> None:
    base = lift({"a": ["x"], "b": ["y"]})
    higher = lift({"a": [], "b": []})
    merged, narrowings = merge_narrowing_allowed(base, higher)
    assert sorted(narrowings) == ["a", "b"]
    assert finalize(merged) == {"a": [], "b": []}


def test_merge_default_over_non_empty_aggregate_narrows() -> None:
    base = lift({"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}})
    higher = lift({"permissions": {"allow": ["X"]}})
    _, narrowings = merge_narrowing_allowed(base, higher)
    assert narrowings == ["permissions"]


def test_merge_nested_dict_value_narrowing_reports_deep_leaf_path() -> None:
    """A same-keys dict whose nested value narrows reports the deep leaf path, not the
    containing field: ``commands.create.defaults`` keeps all keys, but its ``env`` list
    drops an entry, so the narrowing is reported at ``commands.create.defaults.env``."""
    base = lift({"commands": {"create": {"defaults": {"env": ["A", "B"]}}}})
    higher = lift({"commands": {"create": {"defaults": {"env": ["A"]}}}})
    _, narrowings = merge_narrowing_allowed(base, higher)
    assert narrowings == ["commands.create.defaults.env"]


def test_merge_static_payload_suppresses_narrowing() -> None:
    base = lift({"cli_args": ["--debug", "--trace"]})
    higher = lift({"cli_args": StaticList(["--verbose"])})
    merged, narrowings = merge_narrowing_allowed(base, higher)
    assert finalize(merged) == {"cli_args": ["--verbose"]}
    assert narrowings == []


def test_static_dict_is_an_atomic_leaf_through_lift_finalize_lower() -> None:
    """A ``StaticDict`` (a dict subclass) is carried whole -- not descended into as a
    patch -- so it keeps its narrowing exemption: replacing a non-empty base dict with
    a ``StaticDict`` that drops keys does not narrow, and it survives lift/finalize and
    a lower round-trip intact (it is not dissolved into a node patch)."""
    static = StaticDict({"allow": ["X"]})
    # lift keeps it as an atomic leaf payload, not a nested patch.
    assert lift({"permissions": static}) == {"permissions": Default(static)}
    # It round-trips through lower unchanged (no key__... suffixes leak in).
    assert lower(lift({"permissions": static})) == {"permissions": static}
    # And it suppresses narrowing even though it drops base keys.
    base = lift({"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}})
    merged, narrowings = merge_narrowing_allowed(base, lift({"permissions": static}))
    assert narrowings == []
    assert finalize(merged) == {"permissions": {"allow": ["X"]}}


def test_static_dict_as_extend_payload_replaces_atomically() -> None:
    """A ``StaticDict`` used as an ``__extend`` payload is an atomic leaf: it replaces the
    base wholesale rather than being recursed into. (Regression: the extend branches used
    ``isinstance(dict)`` and descended into the marker's raw values, crashing with
    ``AttributeError``.)"""
    # Extend-against-a-patch-base: the Static* value wins atomically.
    base = lift({"a": {"x": [1]}})
    merged, narrowings = merge_narrowing_allowed(base, lift({"a__extend": StaticDict({"y": 2})}))
    assert finalize(merged) == {"a": {"y": 2}}
    assert narrowings == []
    # Extend-against-absent (the original crash repro: a present-but-None base).
    merged2, _ = merge_narrowing_allowed(lift({"a": None}), lift({"a__extend": StaticDict({"x": 1})}))
    assert finalize(merged2) == {"a": {"x": 1}}
    # Extend-over-Extend with a Static* payload: the higher one wins outright.
    merged3, _ = merge_narrowing_allowed(lift({"a__extend": {"x": 1}}), lift({"a__extend": StaticDict({"y": 2})}))
    assert finalize(merged3) == {"a": {"y": 2}}


def test_merge_scalar_tuple_payload_suppresses_narrowing() -> None:
    base = lift({"cidrs": ("0.0.0.0/0",)})
    higher = lift({"cidrs": ScalarTuple(("203.0.113.4/32",))})
    _, narrowings = merge_narrowing_allowed(base, higher)
    assert narrowings == []


def test_merge_assign_payload_suppresses_narrowing() -> None:
    base = lift({"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}})
    bare = lift({"permissions": {"allow": ["X"]}})
    _, bare_narrowings = merge_narrowing_allowed(base, bare)
    assert bare_narrowings == ["permissions"]
    assigned = lift({"permissions__assign": {"allow": ["X"]}})
    _, assign_narrowings = merge_narrowing_allowed(base, assigned)
    assert assign_narrowings == []


def test_merge_extend_against_base_does_not_narrow() -> None:
    base = lift({"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}})
    higher = lift({"permissions__extend": {"allow__extend": ["X"]}})
    merged, narrowings = merge_narrowing_allowed(base, higher)
    assert finalize(merged) == {"permissions": {"defaultMode": "acceptEdits", "allow": ["old", "X"]}}
    assert narrowings == []


def test_merge_assigns_absent_key_without_narrowing() -> None:
    base = lift({"permissions": {"defaultMode": "acceptEdits"}})
    higher = lift({"model": "opus"})
    merged, narrowings = merge_narrowing_allowed(base, higher)
    assert finalize(merged) == {"permissions": {"defaultMode": "acceptEdits"}, "model": "opus"}
    assert narrowings == []


def test_merge_nested_bare_drop_inside_extend_is_recorded() -> None:
    """The recursive-narrowing case: an ``__extend`` never narrows at its own level,
    but a bare (``Default``) assign nested inside the extend payload that drops a lower
    aggregate is still recorded at its deep dotted path. ``assign_drops`` must thread
    through ``apply_extend``'s internal ``combine`` for this to be caught."""
    base = lift({"foo": {"bar": ["Y", "Z"]}})
    higher = lift({"foo__extend": {"bar": ["X"]}})
    merged, narrowings = merge_narrowing_allowed(base, higher)
    assert narrowings == ["foo.bar"]
    assert finalize(merged) == {"foo": {"bar": ["X"]}}


def test_merge_nested_add_inside_extend_does_not_narrow() -> None:
    """A sibling key added inside an ``__extend`` is a pure addition: it preserves the
    untouched sibling and records no narrowing."""
    base = lift({"foo": {"bar": ["Y", "Z"]}})
    higher = lift({"foo__extend": {"baz": ["W"]}})
    merged, narrowings = merge_narrowing_allowed(base, higher)
    assert narrowings == []
    assert finalize(merged) == {"foo": {"bar": ["Y", "Z"], "baz": ["W"]}}


def test_merge_nested_extend_inside_extend_does_not_narrow() -> None:
    """A nested ``__extend`` inside an ``__extend`` is a superset at every level and
    never narrows."""
    base = lift({"foo": {"bar": ["Y", "Z"]}})
    higher = lift({"foo__extend": {"bar__extend": ["X"]}})
    merged, narrowings = merge_narrowing_allowed(base, higher)
    assert narrowings == []
    assert finalize(merged) == {"foo": {"bar": ["Y", "Z", "X"]}}


def test_merge_nested_drop_when_combining_two_extends_is_recorded() -> None:
    """Combining two deferred ``Extend`` patches threads ``assign_drops`` too: a higher
    bare assign nested in the upper extend that drops the lower extend's value narrows."""
    lower = lift({"foo__extend": {"bar": ["Y", "Z"]}})
    higher = lift({"foo__extend": {"bar": ["X"]}})
    merged, narrowings = merge_narrowing_allowed(lower, higher)
    assert narrowings == ["foo.bar"]
    assert finalize(merged) == {"foo": {"bar": ["X"]}}


# =============================================================================
# merge against a concrete base -- round-trips
# =============================================================================


def test_merge_against_concrete_base_extend_round_trip() -> None:
    base = {"model": "opus", "permissions": {"allow": ["old"]}}
    higher = lift({"permissions__extend": {"allow__extend": ["X"]}})
    merged, narrowings = merge_narrowing_allowed(lift(base), higher)
    assert finalize(merged) == {"model": "opus", "permissions": {"allow": ["old", "X"]}}
    assert narrowings == []


def test_merge_against_concrete_base_default_round_trip() -> None:
    base = {"model": "opus"}
    higher = lift({"model": "sonnet"})
    merged = merge(lift(base), higher)
    assert finalize(merged) == {"model": "sonnet"}


# =============================================================================
# associativity -- finalize(merge(merge(B, X), Y)) == finalize(merge(B, merge(X, Y)))
# =============================================================================


def _assoc_left(base: Patch, lower: Patch, higher: Patch) -> dict[str, Any]:
    return finalize(merge_narrowing_allowed(merge_narrowing_allowed(base, lower)[0], higher)[0])


def _assoc_right(base: Patch, lower: Patch, higher: Patch) -> dict[str, Any]:
    return finalize(merge_narrowing_allowed(base, merge_narrowing_allowed(lower, higher)[0])[0])


@pytest.mark.parametrize(
    ("base", "lower", "higher"),
    [
        ({"f": ["V"]}, {"f__extend": ["A"]}, {"f__extend": ["B"]}),
        ({"f": ["V"]}, {"f": ["A"]}, {"f__extend": ["B"]}),
        ({"f": ["V"]}, {"f__extend": ["A"]}, {"f": ["B"]}),
        ({"f": ["V"]}, {"f": ["A"]}, {"f": ["B"]}),
        ({"f": ["V"]}, {"f__extend": ["A"]}, {"f__assign": ["B"]}),
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
    ],
)
def test_merge_is_associative_under_finalize(
    base: dict[str, Any], lower: dict[str, Any], higher: dict[str, Any]
) -> None:
    """``finalize(merge(merge(B, X), Y)) == finalize(merge(B, merge(X, Y)))`` over node
    patches, for the combine cases plus nested-dict recursion and ``__assign``."""
    base_patch = lift(base)
    lower_patch = lift(lower)
    higher_patch = lift(higher)
    assert _assoc_left(base_patch, lower_patch, higher_patch) == _assoc_right(base_patch, lower_patch, higher_patch)


# =============================================================================
# extend_plain_value -- plain-dict boundary into the node extend algebra
# =============================================================================


def test_extend_plain_value_concats_lists() -> None:
    assert extend_plain_value(["A"], ["B"], "f") == ["A", "B"]


def test_extend_plain_value_unions_sets() -> None:
    assert extend_plain_value({"A"}, ["B"], "f") == {"A", "B"}


def test_extend_plain_value_preserves_tuple_type() -> None:
    result = extend_plain_value(("A",), ("B",), "f")
    assert result == ("A", "B")
    assert isinstance(result, tuple)


def test_extend_plain_value_recurses_into_nested_extend() -> None:
    """A nested ``key__extend`` extends the matching base sub-value."""
    base = {"defaultMode": "acceptEdits", "allow": ["old"]}
    extend = {"allow__extend": ["new"]}
    assert extend_plain_value(base, extend, "permissions") == {
        "defaultMode": "acceptEdits",
        "allow": ["old", "new"],
    }


def test_extend_plain_value_nested_bare_key_assigns_replacing_its_subtree() -> None:
    """A nested bare key assigns at its level, replacing that whole sub-dict; top-level
    siblings of the assigned key are preserved."""
    base = {"a": {"x": 1, "y": 2}, "keep": ["Z"]}
    assert extend_plain_value(base, {"a": {"x": 9}}, "f") == {"a": {"x": 9}, "keep": ["Z"]}


def test_extend_plain_value_against_none_assigns_aggregate() -> None:
    assert extend_plain_value(None, ["A"], "f") == ["A"]


def test_extend_plain_value_against_none_resolves_nested_markers() -> None:
    assert extend_plain_value(None, {"allow__extend": ["X"]}, "f") == {"allow": ["X"]}


def test_extend_plain_value_rejects_extend_on_scalar() -> None:
    with pytest.raises(OverlayError, match="target field is a scalar"):
        extend_plain_value("base", "oops", "f")


def test_extend_plain_value_conflicting_assign_names_the_field_path() -> None:
    # A bare key and its __assign form for the same field at the top level of the extend
    # body are contradictory; the error names the dotted field_path so the offending key
    # is locatable (the location the plain-dict resolver used to surface).
    with pytest.raises(OverlayError, match=r"Conflicting assignment at 'create_templates.modal.env'"):
        extend_plain_value({}, {"x": 1, "x__assign": 2}, "create_templates.modal.env")
