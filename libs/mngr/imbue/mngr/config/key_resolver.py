"""Shared resolver for setting overrides.

Recognizes the ``__extend`` suffix on leaf keys and resolves it against the
current config state into a plain assignment. The bare key is always an
assignment; ``key__extend`` means "extend the base value": concat for
list/tuple, recursive key-merge for dict, union for set/frozenset.

The resolver runs before ``parse_config``; the raw dict it returns contains
no ``__extend`` keys, so the parser never has to know about the operator.

A single source of truth for the operator name and the segment separator is
kept here so env-var parsing, TOML parsing, ``--setting`` parsing, and
``mngr config`` all stay in lockstep.
"""

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final

from pydantic import BaseModel

from imbue.mngr.config.data_types import CommandDefaults
from imbue.mngr.config.data_types import CreateTemplate
from imbue.mngr.errors import InvalidKeyPathError
from imbue.overlay.merge import apply_extend
from imbue.overlay.operators import assign_bare_key
from imbue.overlay.operators import bare_key
from imbue.overlay.operators import check_no_conflicting_assign
from imbue.overlay.operators import is_assign_key
from imbue.overlay.operators import is_extend_key


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
    check_no_conflicting_assign(override, ".".join(path))
    result: dict[str, Any] = {}
    # First pass (assign-phase): copy bare and ``__assign`` keys, recursing into
    # nested dicts. ``__assign`` is value-identical to bare here -- the suffix only
    # suppresses narrowing, and ``resolve_extends`` does no narrowing tracking -- so
    # it collapses to the bare field name in the resolved output.
    for key, value in override.items():
        if is_extend_key(key):
            continue
        bare = assign_bare_key(key) if is_assign_key(key) else key
        # Preserve a deferred ``__assign`` verbatim when the base has no value,
        # mirroring the deferred ``__extend`` handling below: the ``key__assign`` is
        # carried to the runtime consumer (``_build_settings_json``), which re-lifts
        # it as a no-warn ``Assign`` instead of a narrowing-checked bare assign.
        # Scoped to paths whose consumer understands ``__assign`` (settings_overrides,
        # not create_templates, whose ``apply_create_template`` reads only ``__extend``).
        if is_assign_key(key) and is_deferred_assign_path(path) and _walk_to_field(base, path + (bare,)) is None:
            result[key] = value
            continue
        if isinstance(value, dict):
            result[bare] = resolve_extends(base, value, path=path + (bare,))
        else:
            result[bare] = value
    # Second pass (extend-phase): apply ``__extend`` keys against either the
    # just-set assign value (if both forms appear in the same layer) or the base.
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
        result[bare] = apply_extend(current, value, field_path)
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


# Deferred paths where a ``__assign`` marker (not only ``__extend``) is also
# preserved for runtime resolution. Scoped to ``settings_overrides``, whose consumer
# (``_build_settings_json``) re-lifts the stored patch and honours the no-warn
# ``Assign``. ``create_templates`` is intentionally excluded: its consumer
# (``apply_create_template``) reads only ``__extend``, so a preserved ``__assign``
# there would surface as a literal option key.
_DEFERRED_ASSIGN_MATCHERS: Final[tuple[_PrefixMatcher, ...]] = (
    _PrefixMatcher(prefix=("agent_types", _WILDCARD_SEGMENT, "settings_overrides")),
)


def is_deferred_assign_path(path: tuple[str, ...]) -> bool:
    """Return True when a ``__assign`` marker at ``path`` should be preserved for
    deferred runtime resolution rather than collapsed to a bare assign at load.

    Distinct from ``is_deferred_extend_path``: deferred ``__assign`` preservation is
    limited to consumers that re-lift the stored patch and understand the no-warn
    assign (``settings_overrides``), so the no-warn intent survives to provision.
    """
    return any(matcher.matches(path) for matcher in _DEFERRED_ASSIGN_MATCHERS)
