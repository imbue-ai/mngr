"""Key-suffix operators and their helpers.

The merge algebra encodes per-key precedence semantics as suffixes on leaf keys:
``key__extend`` (merge onto the layer below), ``key__assign`` (assign without
recording a narrowing), and a bare ``key`` (narrowing-checked assign). These
helpers recognise the suffixes, strip them, and validate that a single layer does
not contain a contradictory bare/``__assign`` pair for the same field.
"""

import json
from collections.abc import Mapping
from typing import Any
from typing import Final

from imbue.overlay.errors import OverlayError
from imbue.overlay.pure import pure

# Operator suffix on a leaf key indicating "extend the current value". Consumers
# compile every surface (TOML, override paths, env vars) down to this single
# lowercase form before handing the dict to the algebra, so there is no
# case-specific variant here.
EXTEND_SUFFIX: Final[str] = "__extend"

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
    every surface that compiles a string into an override value so they all keep
    identical value semantics.
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


def check_no_conflicting_assign(value: Mapping[str, Any], field_path: str = "") -> None:
    """Raise ``OverlayError`` if a dict has both a bare ``key`` and ``key__assign``.

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
        raise OverlayError(
            f"Conflicting assignment{location}: field(s) [{names}] have both a bare key and a "
            f"'{ASSIGN_SUFFIX}' key in the same layer. Use exactly one of bare assign or "
            f"'{ASSIGN_SUFFIX}' (assign without the narrowing check), not both."
        )
