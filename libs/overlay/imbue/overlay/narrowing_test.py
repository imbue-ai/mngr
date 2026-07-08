"""Unit tests for the narrowing predicates (``would_assignment_narrow`` /
``narrowing_paths``). The leaf-extend primitive (``extend_aggregate_leaf``) and the
extend algebra are tested via ``node_merge_test`` (the single extend engine)."""

from imbue.overlay.markers import ScalarTuple
from imbue.overlay.markers import StaticDict
from imbue.overlay.markers import StaticList
from imbue.overlay.markers import StaticTuple
from imbue.overlay.narrowing import narrowing_paths
from imbue.overlay.narrowing import would_assignment_narrow

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


# =============================================================================
# narrowing_paths -- deep-path counterpart of would_assignment_narrow
# =============================================================================


def test_narrowing_paths_top_level_list_drop_reports_field() -> None:
    assert narrowing_paths(["a", "b"], ["c"], "tags") == ["tags"]


def test_narrowing_paths_superset_yields_nothing() -> None:
    assert narrowing_paths(["a"], ["a", "b"], "tags") == []


def test_narrowing_paths_static_override_yields_nothing() -> None:
    assert narrowing_paths(["a", "b"], StaticList(["c"]), "tags") == []


def test_narrowing_paths_dict_dropped_key_reports_dict_field() -> None:
    assert narrowing_paths({"x": 1, "y": 2}, {"x": 1}, "opts") == ["opts"]


def test_narrowing_paths_non_dict_over_dict_reports_dict_field() -> None:
    assert narrowing_paths({"x": 1}, ["x"], "opts") == ["opts"]


def test_narrowing_paths_nested_list_narrowing_reports_deep_path() -> None:
    assert narrowing_paths({"env": ["A", "B"]}, {"env": ["A"]}, "defaults") == ["defaults.env"]


def test_narrowing_paths_doubly_nested_value_narrowing_reports_full_deep_path() -> None:
    base = {"create": {"defaults": {"env": ["A", "B"]}}}
    override = {"create": {"defaults": {"env": ["A"]}}}
    assert narrowing_paths(base, override, "commands") == ["commands.create.defaults.env"]


def test_narrowing_paths_multiple_narrowing_keys_yields_multiple_paths() -> None:
    base = {"env": ["A", "B"], "ports": ["1", "2"], "kept": ["X"]}
    override = {"env": ["A"], "ports": ["1"], "kept": ["X"]}
    assert narrowing_paths(base, override, "defaults") == ["defaults.env", "defaults.ports"]


def test_narrowing_paths_empty_or_scalar_base_yields_nothing() -> None:
    assert narrowing_paths([], ["a"], "tags") == []
    assert narrowing_paths("x", "y", "name") == []


def test_narrowing_paths_set_drop_reports_field_but_superset_does_not() -> None:
    assert narrowing_paths({"a", "b"}, {"a"}, "tags") == ["tags"]
    assert narrowing_paths({"a"}, {"a", "b"}, "tags") == []
