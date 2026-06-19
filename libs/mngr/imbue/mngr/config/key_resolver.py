"""Shared resolver for setting overrides: the one place mngr recognises the operator
suffixes (``__extend`` / ``__assign``) across every surface -- env vars, TOML,
``--setting``, ``mngr config`` -- so they stay in lockstep.

Runs before ``parse_config`` and resolves each marker against the current config state
into a plain assignment, except on the deferred-resolution paths (see
``is_deferred_extend_path``), whose markers are preserved for a runtime base. The raw
dict it returns is otherwise marker-free, so the parser never sees the operators.

For the operator semantics themselves (concat / recursive dict-merge / set-union, the
two-phase within-layer resolution) see the ``imbue.overlay`` README; for mngr's scheme
end-to-end see ``config/README.md``.
"""

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from imbue.mngr.config.data_types import CommandDefaults
from imbue.mngr.config.data_types import CreateTemplate
from imbue.mngr.config.external_settings import desugar_settings_overrides
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.errors import InvalidKeyPathError
from imbue.overlay.errors import OverlayError
from imbue.overlay.node_merge import extend_plain_value
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
    """Resolve ``__extend``-suffixed leaf keys in ``override`` against ``base``.

    Thin boundary wrapper over ``_resolve_extends`` that translates the raw
    ``OverlayError`` the overlay algebra raises for structurally-malformed patches
    (a ``__extend`` on a scalar, a shape-mismatched ``__extend`` value, an
    incompatible marker combination, a bare-plus-``__assign`` conflict) into a
    ``ConfigParseError``. Without this, those errors would escape the config-load /
    ``--setting`` paths as a bare ``OverlayError`` -- not a ``ClickException`` -- and
    surface to the user as an unexpected-error traceback instead of a clean
    ``Error: ...`` message. A ``ConfigParseError`` raised deeper down is not an
    ``OverlayError``, so it propagates unchanged (no double-wrapping).
    """
    try:
        return _resolve_extends(base, override, path=path)
    except OverlayError as e:
        raise ConfigParseError(str(e)) from e


def _resolve_extends(
    base: Any,
    override: dict[str, Any],
    *,
    path: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Walk ``override`` and resolve any ``__extend`` / ``__assign``-suffixed leaf keys
    against ``base``, returning a new dict where every key is a plain assignment.

    Implements mngr's within-layer resolution -- assign-phase then extend-phase (see
    ``config/README.md`` and the ``imbue.overlay`` README for the semantics). Lookups
    against ``base`` traverse pydantic model attributes and Mapping keys interchangeably,
    so the same function works whether ``base`` is a parsed ``MngrConfig``, a nested
    config object, or a raw dict. On a deferred-resolution path a marker is preserved
    verbatim rather than collapsed, so it resolves later against a concrete runtime base
    (a new template's ``env__extend`` against the ``mngr create`` params; a
    ``settings_overrides`` marker against the provision base). A deferred ``__extend``
    (``is_deferred_extend_path``) is preserved only when the base has no value to extend;
    a deferred ``__assign`` (``is_deferred_assign_path``) is preserved unconditionally, so
    the no-warn intent survives even when a lower scope already set the key (the only case
    a narrowing could fire).

    At the root of a ``settings_overrides`` subtree, the Claude-compatible ``__mngr_merge``
    surface is first desugared into the internal suffix form (see
    ``external_settings.desugar_settings_overrides``); the two passes below then process the
    resulting suffix keys as on any other deferred path.
    """
    if _is_settings_overrides_root(path):
        override = desugar_settings_overrides(override, path)
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
        # Preserve a deferred ``__assign`` verbatim: the ``key__assign`` is carried to
        # the runtime consumer (``_build_settings_json``), which re-lifts it as a no-warn
        # ``Assign`` instead of a narrowing-checked bare assign. Unlike the deferred
        # ``__extend`` below, this is NOT gated on the base lacking a value: the no-warn
        # intent matters precisely when a lower scope *did* set the key (the only time a
        # narrowing could fire), so collapsing it to bare there would lose the opt-out and
        # let the cross-scope narrowing guard error on exactly the key the user opted out.
        # Scoped to paths whose consumer understands ``__assign`` (settings_overrides,
        # not create_templates, whose ``apply_create_template`` reads only ``__extend``).
        if is_assign_key(key) and is_deferred_assign_path(path):
            result[key] = value
            continue
        if isinstance(value, dict):
            result[bare] = _resolve_extends(base, value, path=path + (bare,))
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
        result[bare] = extend_plain_value(current, value, field_path)
    return result


# Paths whose markers are *deferred*: preserved verbatim at config-load (when the
# base has no value to extend) and resolved later against a concrete runtime base.
# Each deferred path has a wired consumer:
#   - ``create_templates.<name>`` (exactly two segments) -> ``apply_create_template``
#     (cli/common_opts.py); deeper paths are structurally invalid template bodies and
#     are not deferred.
#   - ``agent_types.<name>.settings_overrides`` (and anything under it) ->
#     ``_build_settings_json`` (mngr_claude/plugin.py), folded against the provision
#     base ``B``. The ``<name>`` segment is dynamic.


def is_settings_overrides_path(path: tuple[str, ...]) -> bool:
    """Return True for any path inside an ``agent_types.<name>.settings_overrides`` subtree.

    ``<name>`` (path[1]) is a user-chosen agent-type name and matches any value; the
    marker may sit directly inside ``settings_overrides`` (path of length 3) or nested
    at any depth under it (length > 3).
    """
    return len(path) >= 3 and path[0] == "agent_types" and path[2] == "settings_overrides"


def _is_settings_overrides_root(path: tuple[str, ...]) -> bool:
    """Return True for the ``agent_types.<name>.settings_overrides`` node itself (length 3).

    The ``__mngr_merge`` map lives only at this root (it lands as a top-level key in the
    Claude ``settings.json``), so the desugar runs here rather than at every nested level.
    """
    return len(path) == 3 and is_settings_overrides_path(path)


def _is_create_template_option_path(path: tuple[str, ...]) -> bool:
    """Return True for a ``create_templates.<name>`` option key (exactly two segments)."""
    return len(path) == 2 and path[0] == "create_templates"


def is_deferred_extend_path(path: tuple[str, ...]) -> bool:
    """Return True when ``path`` lies in a deferred-``__extend`` subtree.

    Used by ``resolve_extends`` to recognise leaf keys that should keep their
    ``__extend`` suffix for deferred runtime resolution rather than being eagerly
    resolved against the config-load-time base. The two deferred subtrees are
    ``create_templates.<name>`` (resolved at ``mngr create`` time) and
    ``agent_types.<name>.settings_overrides`` (resolved at provision time).
    """
    return _is_create_template_option_path(path) or is_settings_overrides_path(path)


def is_deferred_assign_path(path: tuple[str, ...]) -> bool:
    """Return True when a ``__assign`` marker at ``path`` should be preserved for
    deferred runtime resolution rather than collapsed to a bare assign at load.

    Distinct from ``is_deferred_extend_path``: deferred ``__assign`` preservation is
    limited to ``settings_overrides``, whose consumer (``_build_settings_json``)
    re-lifts the stored patch and honours the no-warn ``Assign``, so the no-warn
    intent survives to provision. ``create_templates`` is intentionally excluded: its
    consumer (``apply_create_template``) reads only ``__extend``, so a preserved
    ``__assign`` there would surface as a literal option key.
    """
    return is_settings_overrides_path(path)
