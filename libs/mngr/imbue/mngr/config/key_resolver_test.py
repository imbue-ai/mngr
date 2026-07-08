"""Unit tests for the model-aware ``resolve_extends`` resolver.

The pure, model-free merge algebra (operator helpers, the typed-node ``lift`` /
``merge`` / ``finalize`` engine, ``apply_extend`` / ``would_assignment_narrow``, and
the ``Static*`` markers) is tested in ``imbue.overlay``. These tests cover what stays
in mngr: resolving
``__extend`` against a parsed ``MngrConfig`` (walking pydantic models,
``CommandDefaults`` / ``CreateTemplate`` wrappers) and the deferred-path carveouts.
"""

from typing import Any

import pytest

from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import CommandDefaults
from imbue.mngr.config.data_types import CreateTemplate
from imbue.mngr.config.data_types import CreateTemplateName
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import WorkDirExtraPathMode
from imbue.mngr.config.key_resolver import resolve_extends
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.primitives import AgentTypeName

# =============================================================================
# resolve_extends -- list/tuple aggregate
# =============================================================================


def test_resolve_extends_appends_to_list_field() -> None:
    """__extend on a list field appends to the base value."""
    base = MngrConfig(unset_vars=["BASE_VAR"])
    resolved = resolve_extends(base, {"unset_vars__extend": ["FROM_EXTEND"]})
    assert resolved == {"unset_vars": ["BASE_VAR", "FROM_EXTEND"]}


def test_resolve_extends_assign_then_extend_in_same_layer() -> None:
    """Bare assignment is applied first; sibling __extend stacks on top.

    Concretely, `unset_vars = []` + `unset_vars__extend = ["A"]` resolves to
    ``["A"]`` -- the reset-then-add idiom called out in the spec.
    """
    base = MngrConfig(unset_vars=["OLD_BASE"])
    resolved = resolve_extends(
        base,
        {"unset_vars": [], "unset_vars__extend": ["A"]},
    )
    assert resolved == {"unset_vars": ["A"]}


def test_resolve_extends_appends_to_unset_list_field() -> None:
    """__extend with a None-valued (or absent) base falls back to using the extender directly."""
    # Use a raw dict base where the path is genuinely absent, so we hit the
    # ``current_value is None`` branch in _apply_extend.
    base: dict[str, Any] = {}
    resolved = resolve_extends(base, {"unset_vars__extend": ["NEW"]})
    assert resolved == {"unset_vars": ["NEW"]}


# =============================================================================
# resolve_extends -- dict aggregate
# =============================================================================


def test_resolve_extends_shallow_merges_dict_field() -> None:
    """__extend on a dict field shallow-merges keys; extender wins on collision."""
    base = MngrConfig(work_dir_extra_paths={".venv": WorkDirExtraPathMode.SHARE})
    resolved = resolve_extends(
        base,
        {"work_dir_extra_paths__extend": {".env": "SHARE"}},
    )
    # extender adds .env while preserving .venv from the base; values are
    # serialised through model_dump so the existing entry is rendered as its
    # JSON form ("SHARE") -- exactly what users would write in TOML.
    assert resolved == {"work_dir_extra_paths": {".venv": "SHARE", ".env": "SHARE"}}


# =============================================================================
# resolve_extends -- recursive __extend (nested markers)
# =============================================================================


def test_apply_extend_recurses_into_nested_extend_marker() -> None:
    """A nested ``key__extend`` inside an ``__extend`` value extends the
    corresponding sub-value of the base rather than replacing it.

    Against a base ``{permissions: {defaultMode: D, allow: [old]}}``, the patch
    ``permissions__extend = {allow__extend: [X]}`` must preserve ``defaultMode``
    and concatenate ``allow`` -> ``{defaultMode: D, allow: [old, X]}``.
    """
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}}
    resolved = resolve_extends(
        base,
        {"permissions__extend": {"allow__extend": ["X"]}},
    )
    assert resolved == {"permissions": {"defaultMode": "acceptEdits", "allow": ["old", "X"]}}


def test_apply_extend_nested_bare_key_replaces_sub_value_preserving_siblings() -> None:
    """A nested *bare* key inside an ``__extend`` value assigns (replaces) that
    sub-value while preserving sibling keys of the extended dict.

    Against a base ``{permissions: {defaultMode: D, allow: [old]}}``, the patch
    ``permissions__extend = {allow: [X]}`` replaces ``allow`` (bare = assign)
    but keeps ``defaultMode`` -> ``{defaultMode: D, allow: [X]}``.
    """
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}}
    resolved = resolve_extends(
        base,
        {"permissions__extend": {"allow": ["X"]}},
    )
    assert resolved == {"permissions": {"defaultMode": "acceptEdits", "allow": ["X"]}}


def test_apply_extend_backcompat_no_nested_extend_unchanged() -> None:
    """Back-compat invariant: an ``__extend`` value with NO nested ``__extend``
    markers produces the same result as the old shallow ``{**current, **value}``.

    The recursive change must only add meaning for nested ``__extend`` markers;
    a value of only bare nested keys merges shallowly (bare = replace at that
    level, siblings preserved), identical to the pre-recursion operator.
    """
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}}
    patch = {"permissions__extend": {"allow": ["X"], "deny": ["Y"]}}
    resolved = resolve_extends(base, patch)
    # Old shallow behavior: {**{defaultMode, allow:[old]}, **{allow:[X], deny:[Y]}}
    expected = {"permissions": {"defaultMode": "acceptEdits", "allow": ["X"], "deny": ["Y"]}}
    assert resolved == expected


def test_apply_extend_recurses_three_levels_deep() -> None:
    """A three-level nest of ``__extend`` markers extends at the deepest level
    while preserving siblings at every intermediate level."""
    base: dict[str, Any] = {
        "a": {
            "keepA": 1,
            "b": {
                "keepB": 2,
                "c": [1],
            },
        }
    }
    resolved = resolve_extends(
        base,
        {"a__extend": {"b__extend": {"c__extend": [2, 3]}}},
    )
    assert resolved == {
        "a": {
            "keepA": 1,
            "b": {
                "keepB": 2,
                "c": [1, 2, 3],
            },
        }
    }


def test_apply_extend_nested_bare_dict_resolves_markers_against_empty() -> None:
    """A nested *bare* dict value that itself contains an ``__extend`` marker is
    resolved against an empty base (extend-against-nothing = assign), so no
    marker survives in the assigned sub-value."""
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits"}}
    resolved = resolve_extends(
        base,
        # ``permissions`` is bare (assign), so it replaces the whole sub-dict;
        # its nested ``allow__extend`` resolves against nothing -> bare ``allow``.
        {"permissions": {"allow__extend": ["X"]}},
    )
    assert resolved == {"permissions": {"allow": ["X"]}}


# =============================================================================
# resolve_extends -- error cases
# =============================================================================


def test_resolve_extends_rejects_extend_on_scalar() -> None:
    """__extend on a scalar field raises ConfigParseError with a clear message."""
    base = MngrConfig(prefix="base-")
    with pytest.raises(ConfigParseError, match="__extend on field 'prefix'"):
        resolve_extends(base, {"prefix__extend": "oops"})


def test_resolve_extends_rejects_shape_mismatch_dict_on_list() -> None:
    """An object value used to extend a list field raises ConfigParseError."""
    base = MngrConfig(unset_vars=["BASE"])
    with pytest.raises(ConfigParseError, match="requires a JSON array value"):
        resolve_extends(base, {"unset_vars__extend": {"not": "a list"}})


def test_resolve_extends_rejects_scalar_for_unset_list_field() -> None:
    """__extend with a scalar value on an unset field still raises (must be aggregate).

    Use a raw dict base where the target path is absent so that the resolver
    reaches the ``current_value is None`` branch in apply_extend.
    """
    base: dict[str, Any] = {}
    with pytest.raises(ConfigParseError, match="requires a list, tuple, dict, or set value"):
        resolve_extends(base, {"new_field__extend": "not-a-list"})


def test_resolve_extends_rejects_bare_plus_assign_conflict() -> None:
    """A field with both a bare key and its ``__assign`` twin in the same layer
    raises ConfigParseError (translated from the overlay conflict check)."""
    base = MngrConfig(prefix="base-")
    with pytest.raises(ConfigParseError, match="Conflicting assignment"):
        resolve_extends(base, {"prefix": "a", "prefix__assign": "b"})


def test_resolve_extends_translates_overlay_error_to_config_parse_error() -> None:
    """A structural error from the overlay algebra surfaces as ``ConfigParseError``,
    not a bare ``OverlayError``.

    ``ConfigParseError`` is a ``ClickException`` (via ``MngrError``), so the top-level
    CLI handler renders it as a clean ``Error: ...`` message; a bare ``OverlayError``
    would escape as an unexpected-error traceback.
    """
    base = MngrConfig(prefix="base-")
    with pytest.raises(ConfigParseError):
        resolve_extends(base, {"prefix__extend": "oops"})


# =============================================================================
# resolve_extends -- nested paths
# =============================================================================


def test_resolve_extends_recurses_into_nested_dicts() -> None:
    """Recursion follows the override dict, applying extends at each level it appears."""
    base = MngrConfig.model_construct()
    resolved = resolve_extends(
        base,
        {"logging": {"console_level": "TRACE"}},
    )
    # No __extend keys -- the override passes through unchanged.
    assert resolved == {"logging": {"console_level": "TRACE"}}


def test_resolve_extends_walks_through_command_defaults() -> None:
    """``commands.<name>.<param>__extend`` extends against the merged value stored
    in ``CommandDefaults.defaults[<param>]`` rather than looking for a non-existent
    attribute on the model. Without this transparency, the extend would silently
    act as an assign (since the lookup would return ``None``).
    """
    base = MngrConfig(
        commands={"create": CommandDefaults(defaults={"env": ["X=5"]})},
    )
    resolved = resolve_extends(
        base,
        {"commands": {"create": {"env__extend": ["X=7"]}}},
    )
    assert resolved == {"commands": {"create": {"env": ["X=5", "X=7"]}}}


def test_resolve_extends_walks_through_create_template_options() -> None:
    """``create_templates.<name>.<param>__extend`` extends against the merged value
    stored in ``CreateTemplate.options[<param>]``. Symmetrical to the CommandDefaults
    transparency above -- both wrappers stash arbitrary per-key overrides in an
    inner mapping, so the resolver has to peek through to make ``__extend`` honour
    the existing value.
    """
    base = MngrConfig(
        create_templates={CreateTemplateName("dev"): CreateTemplate(options={"env": ["X=1"]})},
    )
    resolved = resolve_extends(
        base,
        {"create_templates": {"dev": {"env__extend": ["X=2"]}}},
    )
    assert resolved == {"create_templates": {"dev": {"env": ["X=1", "X=2"]}}}


def test_resolve_extends_walks_through_agent_type_direct_attribute() -> None:
    """``agent_types.<name>.<field>__extend`` extends against the value on the
    ``AgentTypeConfig`` attribute. Unlike ``CommandDefaults``/``CreateTemplate``,
    agent-type fields like ``cli_args`` / ``env`` are real model attributes, so the
    generic ``getattr`` path is sufficient -- no special wrapper transparency needed.
    """
    base = MngrConfig(
        agent_types={AgentTypeName("my_claude"): AgentTypeConfig(cli_args=("--debug",))},
    )
    resolved = resolve_extends(
        base,
        {"agent_types": {"my_claude": {"cli_args__extend": ["--verbose"]}}},
    )
    assert resolved == {"agent_types": {"my_claude": {"cli_args": ("--debug", "--verbose")}}}


def test_resolve_extends_preserves_extend_suffix_inside_new_create_template() -> None:
    """Inside a ``create_templates.<name>`` block, an ``<opt>__extend`` whose base
    lookup yields ``None`` (the template is brand-new) is preserved verbatim instead
    of collapsing into a bare assign. ``apply_create_template`` interprets the
    preserved key against the runtime create-command params at template-application
    time.

    Locks in the resolver-level invariant independently of the loader integration
    tests so a future refactor cannot silently drop the preserve-extend branch.
    """
    base = MngrConfig()
    resolved = resolve_extends(
        base,
        {"create_templates": {"coder_local": {"type": "claude", "env__extend": ["X=1"]}}},
    )
    assert resolved == {"create_templates": {"coder_local": {"type": "claude", "env__extend": ["X=1"]}}}


def test_resolve_extends_collapses_extend_inside_existing_create_template() -> None:
    """When the base does already have a value for the create-template option,
    ``<opt>__extend`` is materialised into the bare key via ``_apply_extend``
    (concat-list semantics). Demonstrates that the discriminator for the
    preserve-extend branch is the base lookup yielding ``None`` rather than the
    path itself.
    """
    base = MngrConfig(
        create_templates={CreateTemplateName("dev"): CreateTemplate(options={"env": ["X=1"]})},
    )
    resolved = resolve_extends(
        base,
        {"create_templates": {"dev": {"env__extend": ["X=2"]}}},
    )
    assert resolved == {"create_templates": {"dev": {"env": ["X=1", "X=2"]}}}


def test_resolve_extends_does_not_preserve_extend_outside_create_templates() -> None:
    """The preserve-extend branch is scoped to ``create_templates.<name>`` paths
    only. An ``<opt>__extend`` against a ``None`` base elsewhere
    (``commands.<name>.<opt>__extend`` here) still flows through ``_apply_extend``,
    which treats ``current_value=None`` as assign-via-extend and collapses to a
    bare key. Locks in the depth/scope guard in ``is_deferred_extend_path``.
    """
    base = MngrConfig()
    resolved = resolve_extends(
        base,
        {"commands": {"create": {"env__extend": ["X=1"]}}},
    )
    assert resolved == {"commands": {"create": {"env": ["X=1"]}}}


def test_resolve_extends_preserves_extend_inside_settings_overrides() -> None:
    """A ``__mngr_merge`` directive under ``settings_overrides`` is desugared and, because the
    base lookup yields ``None``, preserved verbatim for the provision-time fold (the
    deferred-path carveout) rather than collapsed to a bare assign. (Desugar mechanics are
    pinned in external_settings_test.)
    """
    base = MngrConfig()
    resolved = resolve_extends(
        base,
        {
            "agent_types": {
                "my_claude": {
                    "settings_overrides": {
                        "permissions": {"allow": ["Bash(npm *)"]},
                        "__mngr_merge": {"permissions.allow": "extend"},
                    }
                }
            }
        },
    )
    assert resolved == {
        "agent_types": {
            "my_claude": {"settings_overrides": {"permissions__extend": {"allow__extend": ["Bash(npm *)"]}}}
        }
    }


def test_resolve_extends_preserves_deep_extend_inside_settings_overrides() -> None:
    """A directive targeting a key nested under ``settings_overrides`` is desugared and
    preserved through the resolver, the same as a top-level one.
    """
    base = MngrConfig()
    override = {
        "agent_types": {
            "my_claude": {
                "settings_overrides": {
                    "hooks": {"SessionStart": [{"x": 1}]},
                    "__mngr_merge": {"hooks.SessionStart": "extend"},
                }
            }
        }
    }
    resolved = resolve_extends(base, override)
    assert resolved == {
        "agent_types": {"my_claude": {"settings_overrides": {"hooks__extend": {"SessionStart__extend": [{"x": 1}]}}}}
    }


def test_resolve_extends_preserves_assign_inside_settings_overrides() -> None:
    """A ``__mngr_merge`` ``assign`` directive desugars to a ``<key>__assign`` and, with a
    ``None`` base lookup, is preserved verbatim (not collapsed to bare), so the no-warn
    intent survives to the provision-time fold where it suppresses the narrowing guard.
    Contrast ``create_templates`` (below), whose consumer reads only ``__extend``.
    """
    base = MngrConfig()
    override = {
        "agent_types": {
            "my_claude": {
                "settings_overrides": {"permissions": {"allow": ["X"]}, "__mngr_merge": {"permissions": "assign"}}
            }
        }
    }
    resolved = resolve_extends(base, override)
    assert resolved == {
        "agent_types": {"my_claude": {"settings_overrides": {"permissions__assign": {"allow": ["X"]}}}}
    }


def test_resolve_extends_preserves_assign_inside_settings_overrides_when_base_set() -> None:
    """A deferred ``__assign`` is preserved verbatim even when the base *already* has a
    value at that key -- which is exactly when a cross-scope narrowing could fire. If the
    suffix collapsed to bare here, a higher scope's ``assign`` over a lower scope's
    ``permissions`` would be flagged as narrowing despite the explicit opt-out.
    """
    base = {
        "agent_types": {
            "my_claude": {"settings_overrides": {"permissions": {"allow": ["A"], "defaultMode": "acceptEdits"}}}
        }
    }
    override = {
        "agent_types": {
            "my_claude": {
                "settings_overrides": {"permissions": {"allow": ["B"]}, "__mngr_merge": {"permissions": "assign"}}
            }
        }
    }
    resolved = resolve_extends(base, override)
    assert resolved == {
        "agent_types": {"my_claude": {"settings_overrides": {"permissions__assign": {"allow": ["B"]}}}}
    }


def test_resolve_extends_collapses_assign_inside_create_templates() -> None:
    """``create_templates`` is excluded from deferred-``__assign`` preservation: its
    consumer (``apply_create_template``) reads only ``__extend``, so a ``__assign``
    there collapses to a bare key at config-load as before."""
    base = MngrConfig()
    override = {"create_templates": {"tmpl": {"env__assign": {"A": "1"}}}}
    resolved = resolve_extends(base, override)
    assert resolved == {"create_templates": {"tmpl": {"env": {"A": "1"}}}}


# =============================================================================
# resolve_extends -- __assign operator (collapses to bare; no narrowing tracking)
# =============================================================================


def test_resolve_extends_assign_key_assigns_like_bare() -> None:
    """``resolve_extends`` collapses ``key__assign`` to the bare field name (it does
    no narrowing tracking, so the suffix has no other effect there)."""
    base = MngrConfig(unset_vars=["OLD"])
    resolved = resolve_extends(base, {"unset_vars__assign": ["NEW"]})
    assert resolved == {"unset_vars": ["NEW"]}


def test_resolve_extends_assign_then_extend_resets_then_adds() -> None:
    base = MngrConfig(unset_vars=["OLD"])
    resolved = resolve_extends(base, {"unset_vars__assign": [], "unset_vars__extend": ["A"]})
    assert resolved == {"unset_vars": ["A"]}


def test_resolve_extends_bare_plus_assign_same_key_raises() -> None:
    with pytest.raises(ConfigParseError, match="Conflicting assignment"):
        resolve_extends(MngrConfig(), {"unset_vars": [], "unset_vars__assign": []})
