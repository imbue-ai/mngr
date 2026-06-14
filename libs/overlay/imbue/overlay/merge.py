"""The core merge algebra: extend application, patch combination, and the unified
``merge`` / ``finalize`` operations.

Everything here operates purely on plain dicts/lists/sets/scalars plus the
key-suffix operators and the ``Static*`` markers. ``apply_extend`` / ``extend_dict``
resolve ``__extend`` against a concrete value; ``combine_patches`` condenses two
marker-carrying patches without a base; ``merge`` wraps ``combine_patches`` with
recursive narrowing detection; ``finalize`` resolves any remaining markers against
an empty base. The narrowing predicate ``would_assignment_narrow`` lives here too,
since ``merge`` filters the raw assign-drop candidates through it.
"""

from collections.abc import Mapping
from typing import Any

from imbue.overlay.errors import OverlayError
from imbue.overlay.markers import is_static_marker
from imbue.overlay.operators import ASSIGN_SUFFIX
from imbue.overlay.operators import EXTEND_SUFFIX
from imbue.overlay.operators import assign_bare_key
from imbue.overlay.operators import bare_key
from imbue.overlay.operators import check_no_conflicting_assign
from imbue.overlay.operators import is_assign_key
from imbue.overlay.operators import is_extend_key
from imbue.overlay.operators import resolved_bare_key

# A candidate bare-assign drop recorded by the combine algebra for narrowing analysis:
# ``(lower_value, higher_value, dotted_path)``. ``combine_patches`` appends one for every
# **bare** (not ``__assign``) assign that overrides a concrete lower value, to a list the
# caller passes in. ``merge`` then filters these through ``would_assignment_narrow`` to
# get the real narrowing paths. Collecting raw candidates here -- rather than running the
# narrowing predicate inline -- keeps the combine step a pure structural transform.
AssignDrop = tuple[Any, Any, str]


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


def combine_patches(
    lower: dict[str, Any],
    higher: dict[str, Any],
    *,
    path: tuple[str, ...] = (),
    assign_drops: list[AssignDrop] | None = None,
) -> dict[str, Any]:
    """Combine two settings "patches" into one, ``higher`` over ``lower``.

    A *patch* is a dict that may carry ``key__extend`` markers at any depth and
    is destined to be resolved (``merge`` + ``finalize``) onto a concrete base
    later. ``combine_patches`` lets the per-layer patches be condensed into a
    single patch *without* a base, preserving / combining their markers so that
    resolving the combined patch against a base ``B`` yields the same result as
    folding each patch onto ``B`` in order (associativity).

    Pure, recursive, and associative. Per key, the four-rule table (with ``f`` the
    bare key and ``f__extend`` the marker). A ``f__assign`` key behaves exactly
    like a bare ``f`` for value purposes (it is an assign), but keeps its suffix in
    the output so a later narrowing pass knows to suppress the check:

    - ``higher`` has an assign for ``f`` (bare ``f`` or ``f__assign``): **assign
      wins** -- ``result``'s entry for ``f`` is ``higher``'s value (recursively
      combined against nothing so its own nested markers stay structured but no
      lower contribution leaks in), under the same key name (preserving any
      ``__assign`` suffix), and any ``lower`` contribution for ``f`` is dropped.
    - ``higher`` has ``f__extend``:
        * ``lower`` has a concrete bare ``f``: ``result[f]`` (bare) =
          ``apply_extend(lower[f], higher_value)`` -- extend the bare value; it
          stays bare.
        * ``lower`` has ``f__extend``: ``result[f__extend]`` (still a marker) =
          the two marker values combined: ``combine_patches`` for dicts, list
          concat / set union for sequence markers.
        * ``lower`` has neither: ``higher``'s ``f__extend`` is preserved verbatim.

    Keys present only in ``lower`` carry through unchanged.
    """
    check_no_conflicting_assign(higher, ".".join(path))
    check_no_conflicting_assign(lower, ".".join(path))
    # ``higher`` assign keys (bare or ``__assign``), mapped bare-name -> output key.
    higher_assign_keys = {resolved_bare_key(key): key for key in higher if not is_extend_key(key)}
    higher_bare_keys = set(higher_assign_keys)

    result: dict[str, Any] = {}

    # Carry through lower keys, except those that ``higher`` overrides or combines
    # into (those are produced from the ``higher`` side below).
    for key, value in lower.items():
        if not _is_lower_key_overridden_by_higher(key, higher_bare_keys, higher):
            result[key] = value

    # Apply higher assign keys (assign wins; recurse a dict value against nothing so
    # its own nested markers stay structured without merging in lower). The output
    # key preserves any ``__assign`` suffix. A *bare* (not ``__assign``) assign that
    # drops a non-empty aggregate from the lower value is recorded as narrowing; an
    # ``__assign`` assign suppresses that check.
    for bare, out_key in higher_assign_keys.items():
        value = higher[out_key]
        if isinstance(value, Mapping):
            resolved_value: Any = combine_patches({}, dict(value), path=path + (bare,))
        else:
            resolved_value = value
        result[out_key] = resolved_value
        if assign_drops is not None and not is_assign_key(out_key) and bare in lower and not is_extend_key(bare):
            assign_drops.append((lower[bare], resolved_value, ".".join(path + (bare,))))

    # Apply higher markers.
    for key, value in higher.items():
        if not is_extend_key(key):
            continue
        bare = bare_key(key)
        field_path = ".".join(path + (bare,))
        if bare in higher_bare_keys:
            # The same higher layer also assigns ``f`` (bare or ``__assign``): this is
            # the within-layer assign-then-extend idiom (assign-phase before
            # extend-phase). Stack the extend onto the just-assigned value, under the
            # same output key (preserving any ``__assign`` suffix). Extend is a
            # superset, so it never narrows.
            out_key = higher_assign_keys[bare]
            assigned_value = result[out_key]
            if isinstance(assigned_value, Mapping) and isinstance(value, Mapping):
                result[out_key] = combine_patches(
                    dict(assigned_value), dict(value), path=path + (bare,), assign_drops=assign_drops
                )
            else:
                result[out_key] = apply_extend(assigned_value, value, field_path)
            continue
        assign_lower_key = f"{bare}{ASSIGN_SUFFIX}"
        if bare in lower and not is_extend_key(bare):
            lower_value = lower[bare]
            if isinstance(lower_value, Mapping) and isinstance(value, Mapping):
                # Both sides are dict *patches* (either may carry nested markers).
                # ``apply_extend`` would treat ``lower_value`` as a marker-free base
                # and copy its nested markers verbatim, so they would later resolve in
                # the wrong precedence order (lower over higher) and break
                # associativity. Recurse via ``combine_patches`` instead, which
                # interleaves both sides' nested markers correctly; the result stays
                # bare (lower's bare ``f`` won the slot). The recorder threads through
                # so a bare key nested in the extend value that drops a lower aggregate
                # is recorded at its dotted path (the recursive-narrowing case).
                result[bare] = combine_patches(
                    dict(lower_value), dict(value), path=path + (bare,), assign_drops=assign_drops
                )
            else:
                # Lower has a concrete bare leaf (list/tuple/set/scalar) -> extend it;
                # stays bare. Leaf shapes carry no nested markers. Extend is a superset
                # so it never narrows.
                result[bare] = apply_extend(lower_value, value, field_path)
        elif assign_lower_key in lower:
            # Lower assigned ``f`` without warning, higher extends it. Extend onto the
            # assigned value (extend is a superset, so still no narrowing) and keep the
            # ``__assign`` suffix so the assign-without-warning intent is preserved.
            lower_value = lower[assign_lower_key]
            if isinstance(lower_value, Mapping) and isinstance(value, Mapping):
                result[assign_lower_key] = combine_patches(
                    dict(lower_value), dict(value), path=path + (bare,), assign_drops=assign_drops
                )
            else:
                result[assign_lower_key] = apply_extend(lower_value, value, field_path)
        elif key in lower:
            # Lower has a marker for the same field -> combine the marker values.
            result[key] = _combine_marker_values(lower[key], value, field_path, path + (bare,))
        else:
            # Lower has neither -> preserve the higher marker verbatim.
            result[key] = value
    return result


def _is_lower_key_overridden_by_higher(
    key: str,
    higher_bare_keys: set[str],
    higher: dict[str, Any],
) -> bool:
    """Return True if a ``lower`` key is superseded or combined-into by ``higher``.

    The corresponding ``result`` entry is then produced from the ``higher`` side
    (a value that wins, or a combined marker), so the lower key is not carried
    through verbatim. ``key`` may be bare, an ``__extend`` marker, or an
    ``__assign`` key; in all cases the comparison is on the bare field name.

    A lower ``__assign`` key combined-into by a higher ``__extend`` is overridden
    here too: the marker pass rewrites it to ``f__assign`` with the extended value
    (preserving the no-warn intent), so it must not also be carried through verbatim.
    """
    bare = resolved_bare_key(key)
    return bare in higher_bare_keys or f"{bare}{EXTEND_SUFFIX}" in higher


def _combine_marker_values(
    lower_value: Any,
    higher_value: Any,
    field_path: str,
    path: tuple[str, ...],
) -> Any:
    """Combine two ``__extend`` marker values (the value side of ``f__extend``).

    Dict markers combine recursively via ``combine_patches`` (preserving nested
    markers); list/tuple markers concatenate; set/frozenset markers union. The
    result is still a marker value (no base has been applied).
    """
    if isinstance(lower_value, Mapping) and isinstance(higher_value, Mapping):
        return combine_patches(dict(lower_value), dict(higher_value), path=path)
    if isinstance(lower_value, (list, tuple)) and isinstance(higher_value, (list, tuple)):
        merged = list(lower_value) + list(higher_value)
        return tuple(merged) if isinstance(lower_value, tuple) else merged
    if isinstance(lower_value, (set, frozenset)) and isinstance(higher_value, (set, frozenset, list, tuple)):
        merged_set = set(lower_value) | set(higher_value)
        return frozenset(merged_set) if isinstance(lower_value, frozenset) else merged_set
    raise OverlayError(
        f"Cannot combine __extend values for field '{field_path}': incompatible shapes "
        f"({type(lower_value).__name__} and {type(higher_value).__name__})."
    )


def merge(lower: dict[str, Any], higher: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Combine two settings patches, ``higher`` over ``lower``, returning the combined
    patch and the dotted paths where a bare assign narrowed a non-empty lower aggregate.

    The unified algebra: the value side is exactly ``combine_patches`` (the four-rule,
    recursive, associative, marker-preserving combine), and the narrowing side records
    -- recursively -- wherever a **bare** assign drops a non-empty aggregate entry from
    the corresponding ``lower`` value. Narrowing is suppressed for ``__assign`` keys and
    ``Static*`` values (both honored by ``would_assignment_narrow``). It is **not** gated
    on any global narrowing policy: that is the caller's decision, which keeps ``merge`` a
    pure total function.

    - Against a **concrete** ``lower`` (no markers), every ``higher`` marker resolves
      where ``lower`` has the key and is preserved where absent (combine semantics);
      pair with ``finalize`` to drop any preserved-against-nothing marker.
    - Against a **patch** ``lower``, unresolvable markers survive for later resolution.

    Associativity (property-tested): ``finalize(merge(merge(B, X), Y)) ==
    finalize(merge(B, merge(X, Y)))``. Pure; never raises for narrowing (only the
    bare-plus-``__assign`` conflict, a parse error, can raise -- via ``combine_patches``).
    """
    assign_drops: list[AssignDrop] = []
    merged = combine_patches(lower, higher, assign_drops=assign_drops)
    # ``combine_patches`` collected every bare-assign-over-concrete-lower candidate;
    # keep only those that actually narrow (``__assign`` keys were already excluded by
    # ``combine_patches``; ``Static*`` values are excluded here via
    # ``would_assignment_narrow``).
    narrowings = [
        dotted
        for lower_value, higher_value, dotted in assign_drops
        if would_assignment_narrow(lower_value, higher_value)
    ]
    return merged, narrowings


def finalize(patch: dict[str, Any]) -> dict[str, Any]:
    """Resolve any remaining ``__extend`` markers in ``patch`` against an empty base
    (extend-against-nothing = assign), recursively, producing a marker-free dict.

    Pure. No assertion: a leftover marker resolving to a bare assign is the correct
    "nothing to extend against" behavior, not a bug (a genuinely-forgotten base shows
    up as missing base keys, which ordinary tests catch). ``extend_dict`` already
    performs exactly this recursive resolve-against-empty, so ``finalize`` is a thin
    name for it at the top level.
    """
    return extend_dict({}, patch, "")
