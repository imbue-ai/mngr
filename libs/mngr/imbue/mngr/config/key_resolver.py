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
from imbue.mngr.config.data_types import would_assignment_narrow
from imbue.mngr.config.key_resolver_primitives import apply_extend
from imbue.mngr.config.key_resolver_primitives import bare_key
from imbue.mngr.config.key_resolver_primitives import extend_dict
from imbue.mngr.config.key_resolver_primitives import is_extend_key
from imbue.mngr.errors import InvalidKeyPathError


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
        result[bare] = apply_extend(current, value, field_path)
    return result


def fold_settings_patch(
    base: dict[str, Any],
    patch: dict[str, Any],
    *,
    path: tuple[str, ...] = (),
) -> tuple[dict[str, Any], list[str]]:
    """Fold a settings ``patch`` onto a **concrete** ``base`` dict, returning the
    merged result and the dotted paths where a bare assign narrowed the base.

    ``base`` must carry no ``__extend`` markers (it is the provision base ``B`` or a
    sub-dict of it). The fold applies mngr's standard per-key rules recursively:

    - **bare ``key``** -> assign: ``result[key]`` becomes the value (a dict value
      is first resolved against an empty base so its own nested markers collapse).
      If the assignment drops a non-empty aggregate entry from ``base[key]``
      (``would_assignment_narrow``), the dotted path is recorded -- **at any
      depth**, including bare keys nested inside an ``__extend`` value.
    - **``key__extend``** -> extend ``base[key]``: a dict-vs-dict extend recurses
      (so nested bare assigns are themselves narrow-checked and their paths bubble
      up); list concat / set union / extend-against-absent reuse ``apply_extend``.
      ``__extend`` merges never narrow (they are supersets).

    The result contains no ``__extend`` markers (every marker resolves against the
    concrete base). Pure; the narrowing list is returned rather than raised so the
    caller can apply the escape hatch.
    """
    result: dict[str, Any] = dict(base)
    narrowings: list[str] = []

    # First pass: bare keys assign (resolving their own nested markers against
    # empty so nothing leaks), narrow-checked against the base value.
    for key, value in patch.items():
        if is_extend_key(key):
            continue
        key_path = path + (key,)
        dotted = ".".join(key_path)
        if isinstance(value, Mapping):
            assigned: Any = extend_dict({}, value, dotted)
        else:
            assigned = value
        if would_assignment_narrow(base.get(key), assigned):
            narrowings.append(dotted)
        result[key] = assigned

    # Second pass: ``key__extend`` keys extend the just-assigned bare value (if the
    # same layer set one) or the base value.
    for key, value in patch.items():
        if not is_extend_key(key):
            continue
        bare = bare_key(key)
        key_path = path + (bare,)
        current = result.get(bare)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            # Recurse so nested bare assigns inside the extend value are
            # narrow-checked and their paths bubble up.
            merged_sub, sub_narrowings = fold_settings_patch(dict(current), dict(value), path=key_path)
            result[bare] = merged_sub
            narrowings.extend(sub_narrowings)
        else:
            result[bare] = apply_extend(current, value, ".".join(key_path))
    return result, narrowings


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
