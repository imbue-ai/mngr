"""The leaf-resolution and narrowing primitives that back the node algebra.

Everything here operates purely on plain dicts/lists/sets/scalars plus the
key-suffix operators and the ``Static*`` markers. ``apply_extend`` / ``extend_dict``
resolve a single ``__extend`` against a concrete value (the payload-level extend the
node algebra reuses); ``would_assignment_narrow`` is the value-level narrowing
predicate and ``narrowing_paths`` its path-collecting counterpart, reporting the
specific narrowed leaf paths. The node algebra in ``node_merge.py`` is the engine that
combines and finalizes whole patches; it calls into these primitives.
"""

from collections.abc import Mapping
from typing import Any

from imbue.overlay.errors import OverlayError
from imbue.overlay.markers import is_static_marker
from imbue.overlay.operators import assign_bare_key
from imbue.overlay.operators import bare_key
from imbue.overlay.operators import check_no_conflicting_assign
from imbue.overlay.operators import is_assign_key
from imbue.overlay.operators import is_extend_key


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
    """
    if not isinstance(base_value, (list, tuple, dict, set, frozenset)) or not base_value:
        return False
    # A ``Static*`` override replaces the whole aggregate as a coherent unit
    # (value-set, not narrowing), regardless of the base aggregate shape.
    if is_static_marker(override_value):
        return False
    if isinstance(base_value, (list, tuple)):
        if isinstance(override_value, (list, tuple)) and all(entry in override_value for entry in base_value):
            return False
        return True
    if isinstance(base_value, (set, frozenset)):
        if isinstance(override_value, (set, frozenset, list, tuple)) and set(base_value) <= set(override_value):
            return False
        return True
    # base_value is a non-empty dict
    if not isinstance(override_value, dict):
        return True
    if any(key not in override_value for key in base_value):
        return True
    return any(would_assignment_narrow(sub_base, override_value[key]) for key, sub_base in base_value.items())


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


def apply_extend(
    current_value: Any,
    extend_value: Any,
    field_path: str,
) -> Any:
    """Apply ``extend_value`` onto ``current_value`` and return the result.

    The list/tuple/set/scalar branches operate only at the leaf field. The dict
    branch is **recursive**: each key of ``extend_value`` is applied against the
    matching sub-value of ``current_value`` -- a nested ``key__extend`` recurses
    (extending ``current_value[key]``), while a nested bare ``key`` assigns
    (replacing ``current_value[key]``). A bare value that is itself a dict is
    resolved against an empty base so any markers nested inside it collapse
    (extend-against-nothing = assign). Within a level, bare keys are applied
    before sibling ``__extend`` keys.

    This recursion is backward-compatible: an ``extend_value`` that nests no
    ``__extend`` markers produces the same shallow ``{**current, **value}``
    result (bare nested keys replace at their level, preserving siblings of the
    extended dict).
    """
    if current_value is None:
        # Field unset in base. Extend acts like assign, but the shape must
        # still be an aggregate; scalars cannot be the value of ``__extend``.
        if not isinstance(extend_value, (list, tuple, dict, set, frozenset)):
            raise OverlayError(
                f"__extend on field '{field_path}' requires a list, tuple, dict, or set value; "
                f"got: {type(extend_value).__name__}"
            )
        # Extend-against-nothing acts as assign, but a dict value may still carry
        # nested markers that must resolve (against an empty base) so none leak.
        if isinstance(extend_value, Mapping):
            return extend_dict({}, extend_value, field_path)
        return extend_value
    if isinstance(current_value, (list, tuple)):
        if not isinstance(extend_value, (list, tuple)):
            raise OverlayError(
                f"__extend on field '{field_path}' (list/tuple) requires a JSON array value; "
                f"got: {type(extend_value).__name__}"
            )
        merged = list(current_value) + list(extend_value)
        return tuple(merged) if isinstance(current_value, tuple) else merged
    if isinstance(current_value, (set, frozenset)):
        if not isinstance(extend_value, (list, tuple, set, frozenset)):
            raise OverlayError(
                f"__extend on field '{field_path}' (set) requires a JSON array value; "
                f"got: {type(extend_value).__name__}"
            )
        merged_set = set(current_value) | set(extend_value)
        return frozenset(merged_set) if isinstance(current_value, frozenset) else merged_set
    if isinstance(current_value, Mapping):
        if not isinstance(extend_value, Mapping):
            raise OverlayError(
                f"__extend on field '{field_path}' (dict) requires a JSON object value; "
                f"got: {type(extend_value).__name__}"
            )
        return extend_dict(current_value, extend_value, field_path)
    raise OverlayError(
        f"__extend on field '{field_path}' is not valid: target field is a scalar "
        f"({type(current_value).__name__}); use bare assignment instead."
    )


def extend_dict(
    current_value: Mapping[str, Any],
    extend_value: Mapping[str, Any],
    field_path: str,
) -> dict[str, Any]:
    """Recursively apply a dict ``extend_value`` onto ``current_value``.

    Each key of ``extend_value`` is applied against the matching sub-value of
    ``current_value``: a nested ``key__extend`` recurses via ``apply_extend``
    (extending ``current_value[key]``); a nested bare ``key`` assigns, replacing
    ``current_value[key]`` (a dict value is resolved against an empty base so any
    markers nested inside it collapse). Bare keys are applied before sibling
    ``__extend`` keys (the within-level assign-phase / extend-phase ordering).

    The result starts as a shallow copy of ``current_value`` so sibling keys that
    the patch does not mention are preserved.
    """
    check_no_conflicting_assign(extend_value, field_path)
    result: dict[str, Any] = dict(current_value)
    # First pass (assign-phase): bare and ``__assign`` keys assign (resolving
    # nested markers against empty). ``__assign`` is value-identical to bare here;
    # the suffix only suppresses narrowing, which this marker-free merge never tracks.
    for key, value in extend_value.items():
        if is_extend_key(key):
            continue
        bare = assign_bare_key(key) if is_assign_key(key) else key
        if isinstance(value, Mapping):
            result[bare] = extend_dict({}, value, f"{field_path}.{bare}")
        else:
            result[bare] = value
    # Second pass (extend-phase): ``key__extend`` keys extend the (possibly
    # just-assigned) value.
    for key, value in extend_value.items():
        if not is_extend_key(key):
            continue
        bare = bare_key(key)
        result[bare] = apply_extend(result.get(bare), value, f"{field_path}.{bare}")
    return result
