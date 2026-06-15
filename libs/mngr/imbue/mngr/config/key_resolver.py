"""Shared resolver for setting overrides.

Recognizes the ``__extend`` suffix on leaf keys and resolves it against the
current config state into a plain assignment. The bare key is always an
assignment; ``key__extend`` means "extend the base value": concat for
list/tuple, shallow key-merge for dict, union for set/frozenset.

The resolver runs before ``parse_config``; the raw dict it returns contains
no ``__extend`` keys, so the parser never has to know about the operator.

A single source of truth for the operator name and the segment separator is
kept here so env-var parsing, TOML parsing, ``--setting`` parsing, and
``mngr config`` all stay in lockstep.
"""

import json
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final

from pydantic import BaseModel

from imbue.imbue_common.pure import pure
from imbue.mngr.config.data_types import CommandDefaults
from imbue.mngr.config.data_types import CreateTemplate
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.errors import InvalidKeyPathError

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


def set_at_path(data: dict[str, Any], key_path: Sequence[str], value: Any) -> None:
    """Set ``value`` at the nested ``key_path`` inside ``data``.

    Creates intermediate dicts as needed. Non-dict intermediate values along
    the path are replaced with fresh dicts -- the override wins by
    construction, which matches the layered-merge model where higher-precedence
    layers overwrite earlier values rather than try to merge into them.

    Shared by the env-var loader, ``--setting`` parsing, and the
    preserved-alias synthesis step so all three entry points produce the same
    raw-dict shape.
    """
    if not key_path:
        raise InvalidKeyPathError("key_path must contain at least one segment")
    current = data
    for segment in key_path[:-1]:
        existing = current.get(segment)
        if not isinstance(existing, dict):
            new_dict: dict[str, Any] = {}
            current[segment] = new_dict
            current = new_dict
        else:
            current = existing
    current[key_path[-1]] = value


def _walk_to_field(base: Any, path: tuple[str, ...]) -> Any:
    """Walk ``base`` along ``path`` and return the value, or None if any
    intermediate step is missing or untraversable.

    Pydantic models are walked via ``getattr`` (natural attribute access);
    Mapping values are walked via ``.get``. The earlier ``model_dump``
    round-trip kept the dynamic-attribute-access ratchet quiet but cost a
    full model serialisation on every override application, which adds up
    when MngrConfig and its plugin sub-configs grow. The direct walk is
    cheap and simpler; we bump the getattr ratchet by one for it.

    ``CommandDefaults`` and ``CreateTemplate`` are exposed transparently:
    both stash arbitrary per-key overrides inside a ``defaults`` / ``options``
    mapping rather than as direct attributes, so ``commands.<name>.<param>``
    and ``create_templates.<name>.<param>`` paths would otherwise silently
    fall through to ``None`` (causing ``__extend`` to act as assign). When
    the segment is not a model field on the wrapper, fall back to the
    appropriate inner mapping so the extend resolves against the real base
    value.
    """
    current: Any = base
    for segment in path:
        if current is None:
            return None
        if isinstance(current, CommandDefaults) and segment not in current.__class__.model_fields:
            current = current.defaults.get(segment)
        elif isinstance(current, CreateTemplate) and segment not in current.__class__.model_fields:
            current = current.options.get(segment)
        elif isinstance(current, BaseModel):
            current = getattr(current, segment, None)
        elif isinstance(current, Mapping):
            current = current.get(segment)
        else:
            return None
    return current


def _apply_extend(
    current_value: Any,
    extend_value: Any,
    field_path: str,
) -> Any:
    """Apply ``extend_value`` onto ``current_value`` and return the result.

    Operates only at the leaf field, no recursion into nested aggregates.
    """
    if current_value is None:
        # Field unset in base. Extend acts like assign, but the shape must
        # still be an aggregate; scalars cannot be the value of ``__extend``.
        if not isinstance(extend_value, (list, tuple, dict, set, frozenset)):
            raise ConfigParseError(
                f"__extend on field '{field_path}' requires a list, tuple, dict, or set value; "
                f"got: {type(extend_value).__name__}"
            )
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
        return {**current_value, **extend_value}
    raise ConfigParseError(
        f"__extend on field '{field_path}' is not valid: target field is a scalar "
        f"({type(current_value).__name__}); use bare assignment instead."
    )


def resolve_extends(
    base: Any,
    override: dict[str, Any],
    *,
    path: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Walk ``override`` and resolve any ``__extend``-suffixed leaf keys
    against ``base``, returning a new dict where every key is a plain
    assignment.

    Within a single layer of ``override``, a bare ``key`` is applied first
    if present, and the sibling ``key__extend`` (if also present) extends
    the just-assigned value. Lookups against ``base`` traverse pydantic
    model attributes and Mapping keys interchangeably, so the same
    function works whether ``base`` is a parsed ``MngrConfig``, a nested
    config object, or a raw dict.

    Inside a ``create_templates.<name>`` block, an ``<opt>__extend`` whose
    base lookup yields ``None`` is preserved verbatim rather than collapsed
    into a bare assign. Template options are applied lazily at
    ``mngr create`` time, so a brand-new template's ``env__extend`` should
    remain an extend (against the runtime command's params) instead of
    silently becoming an assign that would narrow them.
    """
    result: dict[str, Any] = {}
    # First pass: copy bare keys and recurse into nested dicts.
    for key, value in override.items():
        if is_extend_key(key):
            continue
        if isinstance(value, dict):
            result[key] = resolve_extends(base, value, path=path + (key,))
        else:
            result[key] = value
    # Second pass: apply ``__extend`` keys against either the just-set bare
    # value (if both forms appear in the same layer) or the base lookup.
    for key, value in override.items():
        if not is_extend_key(key):
            continue
        bare = bare_key(key)
        field_path = ".".join(path + (bare,))
        if bare in result:
            current = result[bare]
        else:
            current = _walk_to_field(base, path + (bare,))
        # Preserve the __extend suffix inside a create template when the base
        # has no value to extend. apply_create_template will resolve it against
        # the create command's runtime params instead of collapsing to assign.
        if current is None and _is_create_template_option_path(path):
            result[key] = value
            continue
        result[bare] = _apply_extend(current, value, field_path)
    return result


def _is_create_template_option_path(path: tuple[str, ...]) -> bool:
    """Return True when ``path`` is the options of a create template.

    Used by ``resolve_extends`` to recognise leaf keys that should keep their
    ``__extend`` suffix for deferred runtime resolution by
    ``apply_create_template`` rather than being eagerly resolved against the
    config-load-time base.

    Template options live exactly one level inside the ``create_templates``
    container -- ``('create_templates', '<name>')`` -- so the check is on the
    exact depth rather than a prefix match. Deeper paths would mean a
    structurally invalid template body (rejected by ``_parse_create_templates``)
    and should not silently get the preserve-extend treatment.
    """
    return len(path) == 2 and path[0] == "create_templates"
