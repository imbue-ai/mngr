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
    result: dict[str, Any] = dict(current_value)
    # First pass: bare keys assign (resolving nested markers against empty).
    for key, value in extend_value.items():
        if is_extend_key(key):
            continue
        if isinstance(value, Mapping):
            result[key] = extend_dict({}, value, f"{field_path}.{key}")
        else:
            result[key] = value
    # Second pass: ``key__extend`` keys extend the (possibly just-assigned) value.
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
    bare key and ``f__extend`` the marker):

    - ``higher`` has bare ``f``: **bare wins** -- ``result[f]`` is ``higher``'s
      value (recursively combined against nothing so its own nested markers stay
      structured but no lower contribution leaks in), and any ``lower[f__extend]``
      for the same key is dropped.
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
    # ``higher`` bare keys that supersede any same-named lower marker.
    higher_bare_keys = {key for key in higher if not is_extend_key(key)}

    result: dict[str, Any] = {}

    # Carry through lower keys, except those that ``higher`` overrides or combines
    # into (those are produced from the ``higher`` side below).
    for key, value in lower.items():
        if not _is_lower_key_overridden_by_higher(key, higher_bare_keys, higher):
            result[key] = value

    # Apply higher bare keys (bare wins; recurse a dict value against nothing so
    # its own nested markers stay structured without merging in lower).
    for key in higher_bare_keys:
        value = higher[key]
        if isinstance(value, Mapping):
            result[key] = combine_patches({}, dict(value), path=path + (key,))
        else:
            result[key] = value

    # Apply higher markers.
    for key, value in higher.items():
        if not is_extend_key(key):
            continue
        bare = bare_key(key)
        if bare in higher_bare_keys:
            # A bare key in the same higher layer already won.
            continue
        field_path = ".".join(path + (bare,))
        if bare in lower and not is_extend_key(bare):
            # Lower has a concrete bare value -> extend it; stays bare.
            result[bare] = apply_extend(lower[bare], value, field_path)
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
    (a bare value that wins, or a combined marker), so the lower key is not carried
    through verbatim. ``key`` may be bare or an ``__extend`` marker; in both cases
    the comparison is on the bare field name.
    """
    bare = bare_key(key) if is_extend_key(key) else key
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
