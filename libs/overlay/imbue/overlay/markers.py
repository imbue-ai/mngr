"""``Static*`` markers for atomic aggregate values.

A ``Static*`` wrapper marks an aggregate (tuple/list/dict) as a coherent whole:
replacing it from a higher-precedence layer is a value-set, not aggregate
narrowing, so it is exempt from the narrowing check. ``ScalarTuple`` narrows the
concept to a tuple-typed value that is semantically a single scalar.
"""

from typing import Any


class StaticTuple(tuple):
    """Marker tuple subclass for an aggregate value that is **atomic**: replacing it
    from a higher-precedence layer is a value-set, not aggregate narrowing, so it is
    exempt from the narrowing check.

    The ``Static*`` family (``StaticTuple`` / ``StaticList`` / ``StaticDict``) lets a
    producer mark an aggregate as a coherent whole that higher layers *replace*
    rather than *narrow*. ``ScalarTuple`` (a scalar-shaped tuple, e.g. a value
    written as a single string and coerced into a tuple) is a ``StaticTuple``.

    **Purity requirement.** Every ``Static*`` marker is a *pure* subclass of its
    builtin aggregate: it adds the atomic / narrowing-exempt semantics but **no**
    state beyond the underlying tuple/list/dict. Consequently a marker round-trips
    losslessly by *re-marking* -- re-wrapping the plain (serialized) form in the same
    ``Static*`` type reproduces the value (``StaticList(list(x)) == x``). A consumer
    that runs narrowing detection over serialized dicts (where ``model_dump`` has
    stripped the subclass back to a plain aggregate) relies on this: it records which
    paths held a marker on the live value, then re-marks the plain value at each path.
    Do not add instance state to these markers, or that re-marking stops being a no-op.
    """


class StaticList(list):
    """Marker list subclass for an atomic list aggregate, exempt from narrowing.

    See ``StaticTuple``: a higher-precedence layer replacing a ``StaticList`` is a
    value-set, not aggregate narrowing.
    """


class StaticDict(dict):
    """Marker dict subclass for an atomic dict aggregate, exempt from narrowing.

    See ``StaticTuple``: a higher-precedence layer replacing a ``StaticDict`` is a
    value-set, not aggregate narrowing.
    """


class ScalarTuple(StaticTuple):
    """Marker tuple subclass for a tuple-typed value that is semantically a single
    scalar: replacing it from a higher-precedence layer is scalar replacement, not
    aggregate narrowing.

    The narrowing guard normally flags a higher-precedence layer that drops entries
    from a non-empty list/tuple set by a lower layer. For some values that additive
    intent never applies -- the value is a coherent whole a higher layer means to
    *replace* (e.g. a tuple field written as a single string, or a field that is
    always replace-by-default). Marking such a value ``ScalarTuple`` exempts it from
    that check. As a ``StaticTuple`` subclass it is covered by the same
    ``is_static_marker`` exemption.
    """


def is_static_marker(value: Any) -> bool:
    """Return True if ``value`` is a ``Static*`` marker (atomic aggregate).

    A ``Static*`` override is a value-set, not narrowing: it replaces the whole
    aggregate as a coherent unit. Covers ``StaticTuple`` (and its ``ScalarTuple``
    subclasses), ``StaticList``, and ``StaticDict``.
    """
    return isinstance(value, (StaticTuple, StaticList, StaticDict))
