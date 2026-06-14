"""Pure ``__extend`` primitives, free of any config-model dependency.

These functions operate on plain dicts/lists/sets and the ``__extend`` operator
suffix. They are split out from ``key_resolver`` (which additionally walks parsed
config models) so that ``data_types`` -- the layer that *defines* the config
models -- can use ``combine_patches`` without a circular import (``key_resolver``
imports ``data_types``; ``data_types`` imports only these primitives).

``key_resolver`` re-exports the operator helpers (``EXTEND_SUFFIX``,
``is_extend_key``, ``bare_key``) so existing import sites keep working.
"""

import json
from collections.abc import Mapping
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import ConfigParseError

# Operator suffix on a leaf key indicating "extend the current value".
# Lowercase form used in TOML, ``--setting`` paths, and ``mngr config`` keys.
EXTEND_SUFFIX: Final[str] = "__extend"

# Uppercase form for env-var path segments. Matches the all-uppercase
# convention for ``MNGR__*`` segments.
EXTEND_SUFFIX_ENV: Final[str] = "__EXTEND"

# Operator suffix on a leaf key indicating "assign the value, but do not record a
# narrowing violation". Behaviorally identical to a bare assign (replace the value
# from the layer below) except the narrowing check is suppressed for this key --
# the explicit "I am replacing this, don't warn" opt-out. Resolves in the
# assign-phase (alongside bare keys), before any sibling ``__extend``.
ASSIGN_SUFFIX: Final[str] = "__assign"


@pure
def parse_scalar_value(value_str: str) -> Any:
    """Parse a raw string into the appropriate Python scalar/aggregate value.

    JSON-parses first (so booleans, numbers, arrays, and objects work) and
    falls back to the raw string when the input is not valid JSON. Shared by
    the env-var loader, ``--setting``, ``mngr config set/extend``, and tests
    so they all keep identical value semantics.
    """
    try:
        return json.loads(value_str)
    except json.JSONDecodeError:
        return value_str


def is_extend_key(key: str) -> bool:
    """Return True if ``key`` is an ``__extend``-suffixed leaf key.

    The suffix must be preceded by at least one character so ``__extend``
    on its own is not treated as a bare key whose ``bare`` would be empty.
    """
    return key.endswith(EXTEND_SUFFIX) and len(key) > len(EXTEND_SUFFIX)


def bare_key(extend_key: str) -> str:
    """Return the field name with the ``__extend`` suffix stripped."""
    return extend_key[: -len(EXTEND_SUFFIX)]


def is_assign_key(key: str) -> bool:
    """Return True if ``key`` is an ``__assign``-suffixed leaf key.

    Mirrors ``is_extend_key``: the suffix must be preceded by at least one
    character so a bare ``__assign`` (whose ``assign_bare_key`` would be empty)
    is not treated as an assign key.
    """
    return key.endswith(ASSIGN_SUFFIX) and len(key) > len(ASSIGN_SUFFIX)


def assign_bare_key(assign_key: str) -> str:
    """Return the field name with the ``__assign`` suffix stripped."""
    return assign_key[: -len(ASSIGN_SUFFIX)]


def resolved_bare_key(key: str) -> str:
    """Return the bare field name for any key, stripping ``__extend``/``__assign``.

    A bare key is returned unchanged. Used to detect operator collisions on the
    same field within one layer.
    """
    if is_extend_key(key):
        return bare_key(key)
    if is_assign_key(key):
        return assign_bare_key(key)
    return key


def check_no_conflicting_assign(value: Mapping[str, Any], field_path: str = "") -> None:
    """Raise ``ConfigParseError`` if a dict has both a bare ``key`` and ``key__assign``.

    The two are contradictory assigns of the same field in the same layer. (Two
    ``key__assign`` or two ``key__extend`` cannot occur -- they would be duplicate
    dict keys.) Checks only this dict level; recursion is handled by the callers
    that descend into nested dicts.
    """
    assign_bare_names = {assign_bare_key(key) for key in value if is_assign_key(key)}
    if not assign_bare_names:
        return
    bare_names = {key for key in value if not is_extend_key(key) and not is_assign_key(key)}
    conflicts = sorted(assign_bare_names & bare_names)
    if conflicts:
        location = f" at '{field_path}'" if field_path else ""
        names = ", ".join(conflicts)
        raise ConfigParseError(
            f"Conflicting assignment{location}: field(s) [{names}] have both a bare key and a "
            f"'{ASSIGN_SUFFIX}' key in the same layer. Use exactly one of bare assign or "
            f"'{ASSIGN_SUFFIX}' (assign without the narrowing check), not both."
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
    before sibling ``__extend`` keys, matching ``resolve_extends``.

    This recursion is backward-compatible: an ``extend_value`` that nests no
    ``__extend`` markers produces the same shallow ``{**current, **value}``
    result as the pre-recursion operator (bare nested keys replace at their
    level, preserving siblings of the extended dict).
    """
    if current_value is None:
        # Field unset in base. Extend acts like assign, but the shape must
        # still be an aggregate; scalars cannot be the value of ``__extend``.
        if not isinstance(extend_value, (list, tuple, dict, set, frozenset)):
            raise ConfigParseError(
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
            raise ConfigParseError(
                f"__extend on field '{field_path}' (list/tuple) requires a JSON array value; "
                f"got: {type(extend_value).__name__}"
            )
        merged = list(current_value) + list(extend_value)
        return tuple(merged) if isinstance(current_value, tuple) else merged
    if isinstance(current_value, (set, frozenset)):
        if not isinstance(extend_value, (list, tuple, set, frozenset)):
            raise ConfigParseError(
                f"__extend on field '{field_path}' (set) requires a JSON array value; "
                f"got: {type(extend_value).__name__}"
            )
        merged_set = set(current_value) | set(extend_value)
        return frozenset(merged_set) if isinstance(current_value, frozenset) else merged_set
    if isinstance(current_value, Mapping):
        if not isinstance(extend_value, Mapping):
            raise ConfigParseError(
                f"__extend on field '{field_path}' (dict) requires a JSON object value; "
                f"got: {type(extend_value).__name__}"
            )
        return extend_dict(current_value, extend_value, field_path)
    raise ConfigParseError(
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
    ``__extend`` keys, mirroring ``resolve_extends``'s within-level ordering.

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
) -> dict[str, Any]:
    """Combine two settings "patches" into one, ``higher`` over ``lower``.

    A *patch* is a dict that may carry ``key__extend`` markers at any depth and
    is destined to be folded (``resolve_extends`` / ``fold_settings_patch``) onto
    a concrete base later. ``combine_patches`` lets the per-scope (and
    per-inheritance-layer) patches be condensed into a single patch *without* a
    base, preserving / combining their markers so that resolving the combined
    patch against a base ``B`` yields the same result as folding each patch onto
    ``B`` in order (associativity).

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
    # key preserves any ``__assign`` suffix.
    for bare, out_key in higher_assign_keys.items():
        value = higher[out_key]
        if isinstance(value, Mapping):
            result[out_key] = combine_patches({}, dict(value), path=path + (bare,))
        else:
            result[out_key] = value

    # Apply higher markers.
    for key, value in higher.items():
        if not is_extend_key(key):
            continue
        bare = bare_key(key)
        if bare in higher_bare_keys:
            # An assign in the same higher layer already won.
            continue
        field_path = ".".join(path + (bare,))
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
                # bare (lower's bare ``f`` won the slot).
                result[bare] = combine_patches(dict(lower_value), dict(value), path=path + (bare,))
            else:
                # Lower has a concrete bare leaf (list/tuple/set/scalar) -> extend it;
                # stays bare. Leaf shapes carry no nested markers.
                result[bare] = apply_extend(lower_value, value, field_path)
        elif assign_lower_key in lower:
            # Lower assigned ``f`` without warning, higher extends it. Extend onto the
            # assigned value (extend is a superset, so still no narrowing) and keep the
            # ``__assign`` suffix so the assign-without-warning intent is preserved.
            lower_value = lower[assign_lower_key]
            if isinstance(lower_value, Mapping) and isinstance(value, Mapping):
                result[assign_lower_key] = combine_patches(dict(lower_value), dict(value), path=path + (bare,))
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
    raise ConfigParseError(
        f"Cannot combine __extend values for field '{field_path}': incompatible shapes "
        f"({type(lower_value).__name__} and {type(higher_value).__name__})."
    )
