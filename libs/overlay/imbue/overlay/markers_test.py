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


def test_static_markers_are_pure_and_round_trip_by_remarking() -> None:
    """The purity requirement (see ``markers.py``): a ``Static*`` marker adds no state
    beyond its builtin aggregate, so re-wrapping the plain (serialized) form in the same
    type reproduces it. This is the no-op round-trip a consumer relies on to re-mark a
    value that ``model_dump`` has stripped back to a plain aggregate."""
    plain_base = {StaticTuple: tuple, ScalarTuple: tuple, StaticList: list, StaticDict: dict}
    for marker in (StaticTuple(("a", "b")), ScalarTuple(("a",)), StaticList(["a", "b"]), StaticDict({"a": 1})):
        plain = plain_base[type(marker)](marker)
        assert not is_static_marker(plain)  # the serialized form has lost the marker
        remarked = type(marker)(plain)  # re-marking reconstructs it
        assert remarked == marker
        assert is_static_marker(remarked)
        # No instance state beyond the aggregate, so re-marking cannot lose anything.
        assert not vars(marker)
