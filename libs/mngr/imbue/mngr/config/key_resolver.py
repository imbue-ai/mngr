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
            return _extend_dict({}, extend_value, field_path)
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
        return _extend_dict(current_value, extend_value, field_path)
    raise ConfigParseError(
        f"__extend on field '{field_path}' is not valid: target field is a scalar "
        f"({type(current_value).__name__}); use bare assignment instead."
    )


def _extend_dict(
    current_value: Mapping[str, Any],
    extend_value: Mapping[str, Any],
    field_path: str,
) -> dict[str, Any]:
    """Recursively apply a dict ``extend_value`` onto ``current_value``.

    Each key of ``extend_value`` is applied against the matching sub-value of
    ``current_value``: a nested ``key__extend`` recurses via ``_apply_extend``
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
            result[key] = _extend_dict({}, value, f"{field_path}.{key}")
        else:
            result[key] = value
    # Second pass: ``key__extend`` keys extend the (possibly just-assigned) value.
    for key, value in extend_value.items():
        if not is_extend_key(key):
            continue
        bare = bare_key(key)
        result[bare] = _apply_extend(result.get(bare), value, f"{field_path}.{bare}")
    return result


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
        # Preserve the __extend suffix inside a deferred-path subtree when the
        # base has no value to extend. The marker is resolved later against a
        # concrete runtime base (create-command params for templates; the
        # provision settings base ``B`` for settings_overrides) rather than
        # collapsing to assign at config-load.
        if current is None and is_deferred_extend_path(path):
            result[key] = value
            continue
        result[bare] = _apply_extend(current, value, field_path)
    return result


class _ExactDepthMatcher(BaseModel):
    """Matches a path of an exact length whose leading segments equal ``prefix``.

    The path must be exactly ``len(prefix) + 1`` segments long (the trailing
    segment is the dynamic container name). Used for ``create_templates``, whose
    options live exactly one level inside the container --
    ``('create_templates', '<name>')`` -- so deeper paths (structurally invalid
    template bodies) are not given the preserve-extend treatment.
    """

    prefix: tuple[str, ...]

    def matches(self, path: tuple[str, ...]) -> bool:
        return len(path) == len(self.prefix) + 1 and path[: len(self.prefix)] == self.prefix


class _PrefixMatcher(BaseModel):
    """Matches any path at or below ``prefix``.

    Used for ``settings_overrides``: a marker living directly inside, or at any
    depth under, ``('agent_types', '<name>', 'settings_overrides')`` is deferred
    to the provision-time fold, so the match is on the prefix rather than an exact
    depth. ``resolve_extends`` passes the path of the dict *containing* the marker,
    which equals the prefix when the marker sits directly inside settings_overrides
    -- hence ``>=`` rather than ``>``. The ``<name>`` segment is dynamic, matched
    by the ``__wildcard__`` sentinel below.
    """

    prefix: tuple[str, ...]

    def matches(self, path: tuple[str, ...]) -> bool:
        if len(path) < len(self.prefix):
            return False
        return all(_segment_matches(expected, actual) for expected, actual in zip(self.prefix, path, strict=False))


# Sentinel marking a path segment whose concrete value is a user-chosen name
# (e.g. the agent-type name or the template name) and so matches any segment.
_WILDCARD_SEGMENT: Final[str] = "__wildcard__"


def _segment_matches(expected: str, actual: str) -> bool:
    return expected == _WILDCARD_SEGMENT or expected == actual


# Registry of paths whose ``__extend`` markers are *deferred*: preserved verbatim
# at config-load (when the base has no value to extend) and resolved later
# against a concrete runtime base. Each entry must have a wired consumer:
#   - ``create_templates.<name>`` -> ``apply_create_template`` (cli/common_opts.py)
#   - ``agent_types.<name>.settings_overrides`` -> ``_build_settings_json``
#     (mngr_claude/plugin.py), folded against the provision base ``B``.
_DEFERRED_EXTEND_MATCHERS: Final[tuple[_ExactDepthMatcher | _PrefixMatcher, ...]] = (
    _ExactDepthMatcher(prefix=("create_templates",)),
    _PrefixMatcher(prefix=("agent_types", _WILDCARD_SEGMENT, "settings_overrides")),
)


def is_deferred_extend_path(path: tuple[str, ...]) -> bool:
    """Return True when ``path`` lies in a deferred-``__extend`` subtree.

    Used by ``resolve_extends`` to recognise leaf keys that should keep their
    ``__extend`` suffix for deferred runtime resolution rather than being eagerly
    resolved against the config-load-time base. See ``_DEFERRED_EXTEND_MATCHERS``
    for the registry of deferred paths and their consumers.
    """
    return any(matcher.matches(path) for matcher in _DEFERRED_EXTEND_MATCHERS)
