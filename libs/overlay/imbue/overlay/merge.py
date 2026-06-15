"""The leaf-extend primitive and the shared narrowing predicates.

Everything here operates purely on plain dicts/lists/sets/scalars plus the ``Static*``
markers. ``extend_aggregate_leaf`` is the shape-checked leaf branch of the extend algebra
(list concat / set union / scalar error); ``node_merge.py`` -- the single extend engine --
imports it. ``would_assignment_narrow`` is the value-level narrowing predicate and
``narrowing_paths`` its path-collecting counterpart, reporting the specific narrowed leaf
paths; ``node_merge.py`` imports ``narrowing_paths`` from here to expand its recorded
assign drops.
"""

from typing import Any

from imbue.overlay.errors import OverlayError
from imbue.overlay.markers import is_static_marker


def would_assignment_narrow(base_value: Any, override_value: Any) -> bool:
    """Return True if assigning ``override_value`` over ``base_value`` would
    drop at least one base entry (a missing list/set element, a missing dict
    key, or an explicit empty aggregate over a non-empty base).

    No-ops (override equals base) and supersets (every base entry survives, e.g.
    the materialised result of an ``__extend`` operation) return ``False``. A
    ``Static*`` override is also exempt -- it represents replacement of the whole
    aggregate as a coherent unit (a value written as a single scalar, or a value
    declared replace-by-default), not aggregate narrowing. Scalars and
    empty/non-aggregate bases never narrow.

    Equivalent to ``narrowing_paths`` being non-empty: it narrows iff that
    path-collecting counterpart finds at least one narrowed leaf path.
    """
    return bool(narrowing_paths(base_value, override_value, ""))


def narrowing_paths(base_value: Any, override_value: Any, prefix: str) -> list[str]:
    """Return the dotted paths at which assigning ``override_value`` over ``base_value``
    narrows, where ``prefix`` is the dotted path of the value being assigned (the field).

    The path-collecting counterpart to ``would_assignment_narrow`` (same structure, same
    exemptions): the result is non-empty iff ``would_assignment_narrow`` is ``True``. A
    list/set narrowing, a whole-aggregate replacement, or a dropped dict key reports at the
    field ``prefix`` itself; a same-keys dict whose nested values narrow reports the deep
    leaf path of each narrowed value. ``Static*`` overrides, no-ops, supersets, and
    empty/non-aggregate bases yield ``[]``.
    """
    if not isinstance(base_value, (list, tuple, dict, set, frozenset)) or not base_value:
        return []
    if is_static_marker(override_value):
        return []
    if isinstance(base_value, (list, tuple)):
        if isinstance(override_value, (list, tuple)) and all(entry in override_value for entry in base_value):
            return []
        return [prefix]
    if isinstance(base_value, (set, frozenset)):
        if isinstance(override_value, (set, frozenset, list, tuple)) and set(base_value) <= set(override_value):
            return []
        return [prefix]
    # base_value is a non-empty dict
    if not isinstance(override_value, dict):
        return [prefix]
    if any(key not in override_value for key in base_value):
        return [prefix]
    return sum(
        (narrowing_paths(sub_base, override_value[key], f"{prefix}.{key}") for key, sub_base in base_value.items()),
        [],
    )


def extend_aggregate_leaf(current: Any, extend_payload: Any, field_path: str) -> Any:
    """Extend a non-dict aggregate leaf (``current``) by ``extend_payload`` and return it.

    The shape-checked leaf branches shared by both ``apply_extend`` engines (this module's
    plain-dict resolver and ``node_merge.py``'s node algebra): a list/tuple concatenates, a
    set/frozenset unions, and a scalar target is an error. The caller handles the
    ``current is None`` and dict/``Patch`` cases before reaching here.
    """
    if isinstance(current, (list, tuple)):
        if not isinstance(extend_payload, (list, tuple)):
            raise OverlayError(
                f"__extend on field '{field_path}' (list/tuple) requires a JSON array value; "
                f"got: {type(extend_payload).__name__}"
            )
        merged = list(current) + list(extend_payload)
        return tuple(merged) if isinstance(current, tuple) else merged
    if isinstance(current, (set, frozenset)):
        if not isinstance(extend_payload, (list, tuple, set, frozenset)):
            raise OverlayError(
                f"__extend on field '{field_path}' (set) requires a JSON array value; "
                f"got: {type(extend_payload).__name__}"
            )
        merged_set = set(current) | set(extend_payload)
        return frozenset(merged_set) if isinstance(current, frozenset) else merged_set
    raise OverlayError(
        f"__extend on field '{field_path}' is not valid: target field is a scalar "
        f"({type(current).__name__}); use bare assignment instead."
    )
