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
