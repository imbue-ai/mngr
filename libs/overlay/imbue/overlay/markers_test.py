"""Unit tests for the ``Static*`` markers and ``is_static_marker``."""

from imbue.overlay.markers import ScalarTuple
from imbue.overlay.markers import StaticDict
from imbue.overlay.markers import StaticList
from imbue.overlay.markers import StaticTuple
from imbue.overlay.markers import is_static_marker


def test_scalar_tuple_is_a_static_tuple() -> None:
    """``ScalarTuple`` specializes ``StaticTuple`` so the single ``is_static_marker``
    check covers it (and any further subclass) at once."""
    assert isinstance(ScalarTuple(("--verbose",)), StaticTuple)


def test_is_static_marker_recognises_each_static_type() -> None:
    assert is_static_marker(StaticTuple(("a",)))
    assert is_static_marker(StaticList(["a"]))
    assert is_static_marker(StaticDict({"a": 1}))
    assert is_static_marker(ScalarTuple(("a",)))


def test_is_static_marker_rejects_plain_aggregates() -> None:
    assert not is_static_marker(("a",))
    assert not is_static_marker(["a"])
    assert not is_static_marker({"a": 1})
    assert not is_static_marker("scalar")


def _assert_pure_remark_round_trip(marker: object, plain: object) -> None:
    """Assert ``marker`` survives a strip-to-plain then re-mark with no loss.

    ``plain`` is the serialized form (``model_dump`` strips the ``Static*`` subclass
    back to its builtin aggregate). Re-wrapping it in ``type(marker)`` must reproduce
    the marker exactly, which holds only because the marker carries no instance state
    beyond the aggregate.
    """
    assert not is_static_marker(plain)
    remarked = type(marker)(plain)
    assert remarked == marker
    assert is_static_marker(remarked)
    # No instance state beyond the aggregate, so re-marking cannot lose anything.
    assert not vars(marker)


def test_static_markers_are_pure_and_round_trip_by_remarking() -> None:
    """The purity requirement (see ``markers.py``): a ``Static*`` marker adds no state
    beyond its builtin aggregate, so re-wrapping the plain (serialized) form in the same
    type reproduces it. This is the no-op round-trip a consumer relies on to re-mark a
    value that ``model_dump`` has stripped back to a plain aggregate."""
    _assert_pure_remark_round_trip(StaticTuple(("a", "b")), tuple(StaticTuple(("a", "b"))))
    _assert_pure_remark_round_trip(ScalarTuple(("a",)), tuple(ScalarTuple(("a",))))
    _assert_pure_remark_round_trip(StaticList(["a", "b"]), list(StaticList(["a", "b"])))
    _assert_pure_remark_round_trip(StaticDict({"a": 1}), dict(StaticDict({"a": 1})))
