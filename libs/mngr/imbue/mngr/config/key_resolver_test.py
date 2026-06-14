"""Unit tests for the shared assign-vs-extend resolver."""

from typing import Any

import pytest

from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import CommandDefaults
from imbue.mngr.config.data_types import CreateTemplate
from imbue.mngr.config.data_types import CreateTemplateName
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import StaticList
from imbue.mngr.config.data_types import WorkDirExtraPathMode
from imbue.mngr.config.key_resolver import finalize
from imbue.mngr.config.key_resolver import merge
from imbue.mngr.config.key_resolver import resolve_extends
from imbue.mngr.config.key_resolver_primitives import ASSIGN_SUFFIX
from imbue.mngr.config.key_resolver_primitives import EXTEND_SUFFIX
from imbue.mngr.config.key_resolver_primitives import assign_bare_key
from imbue.mngr.config.key_resolver_primitives import bare_key
from imbue.mngr.config.key_resolver_primitives import combine_patches
from imbue.mngr.config.key_resolver_primitives import is_assign_key
from imbue.mngr.config.key_resolver_primitives import is_extend_key
from imbue.mngr.config.key_resolver_primitives import parse_scalar_value
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.primitives import AgentTypeName

# =============================================================================
# is_extend_key / bare_key
# =============================================================================


def test_is_extend_key_recognises_suffix() -> None:
    assert is_extend_key("cli_args__extend")
    assert is_extend_key("a__extend")


def test_is_extend_key_rejects_bare_suffix() -> None:
    """A bare ``__extend`` (no preceding field name) is not a valid extend key."""
    assert not is_extend_key(EXTEND_SUFFIX)


def test_is_extend_key_rejects_plain_field() -> None:
    assert not is_extend_key("cli_args")
    assert not is_extend_key("")


def test_bare_key_strips_suffix() -> None:
    assert bare_key("cli_args__extend") == "cli_args"
    assert bare_key("a__extend") == "a"


# =============================================================================
# parse_scalar_value
# =============================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("false", False),
        ("42", 42),
        ("3.14", 3.14),
        ('"quoted"', "quoted"),
        ("[1, 2, 3]", [1, 2, 3]),
        ('{"k": "v"}', {"k": "v"}),
        ("not_json", "not_json"),
        ("", ""),
    ],
)
def test_parse_scalar_value(raw: str, expected: Any) -> None:
    """JSON-parses first, falls back to the raw string when not valid JSON."""
    assert parse_scalar_value(raw) == expected


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
    reaches the ``current_value is None`` branch in _apply_extend.
    """
    base: dict[str, Any] = {}
    with pytest.raises(ConfigParseError, match="requires a list, tuple, dict, or set value"):
        resolve_extends(base, {"new_field__extend": "not-a-list"})


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
    """An ``<key>__extend`` anywhere under ``agent_types.<name>.settings_overrides``
    whose base lookup yields ``None`` is preserved verbatim through config-load,
    destined for the provision-time fold against the concrete settings base ``B``.

    ``settings_overrides`` is schemaless, so the base lookup is ``None``; without
    the deferred-path carveout the marker would collapse to a bare assign at
    config-load instead of merging onto the home settings.json at provision.
    """
    base = MngrConfig()
    resolved = resolve_extends(
        base,
        {
            "agent_types": {
                "my_claude": {"settings_overrides": {"permissions__extend": {"allow__extend": ["Bash(npm *)"]}}}
            }
        },
    )
    assert resolved == {
        "agent_types": {
            "my_claude": {"settings_overrides": {"permissions__extend": {"allow__extend": ["Bash(npm *)"]}}}
        }
    }


def test_resolve_extends_preserves_deep_extend_inside_settings_overrides() -> None:
    """The settings_overrides carveout is a *prefix* match: a marker nested several
    levels deep under settings_overrides is also preserved, not just a top-level one.
    """
    base = MngrConfig()
    override = {"agent_types": {"my_claude": {"settings_overrides": {"hooks": {"SessionStart__extend": [{"x": 1}]}}}}}
    resolved = resolve_extends(base, override)
    assert resolved == override


# =============================================================================
# combine_patches -- cross-layer patch combine (four-rule table)
# =============================================================================


def test_combine_patches_extend_plus_extend_accumulates_marker() -> None:
    """Row 1: ``f__extend=A`` (lower) + ``f__extend=B`` (higher) -> ``f__extend=A⊕B``.

    The result is still a marker (destined for the base fold), and resolving it
    against a base ``{f: V}`` yields ``V⊕A⊕B`` (associativity, checked below).
    """
    lower = {"f__extend": ["A"]}
    higher = {"f__extend": ["B"]}
    combined = combine_patches(lower, higher)
    assert combined == {"f__extend": ["A", "B"]}


def test_combine_patches_lower_bare_plus_higher_extend_stays_bare() -> None:
    """Row 2: ``f=A`` (lower bare) + ``f__extend=B`` (higher) -> bare ``f=A⊕B``.

    The lower concrete value is extended in place; the key stays bare (no marker)
    because there is a concrete value to fold onto.
    """
    lower = {"f": ["A"]}
    higher = {"f__extend": ["B"]}
    combined = combine_patches(lower, higher)
    assert combined == {"f": ["A", "B"]}


def test_combine_patches_higher_bare_wipes_lower_extend() -> None:
    """Row 3: ``f__extend=A`` (lower) + ``f=B`` (higher bare) -> bare ``f=B``.

    A higher bare key wins outright and drops the lower marker for the same field.
    """
    lower = {"f__extend": ["A"]}
    higher = {"f": ["B"]}
    combined = combine_patches(lower, higher)
    assert combined == {"f": ["B"]}


def test_combine_patches_higher_bare_wipes_lower_bare() -> None:
    """Row 4: ``f=A`` (lower bare) + ``f=B`` (higher bare) -> bare ``f=B``."""
    lower = {"f": ["A"]}
    higher = {"f": ["B"]}
    combined = combine_patches(lower, higher)
    assert combined == {"f": ["B"]}


def test_combine_patches_lower_only_keys_carry_through() -> None:
    """Keys present only in ``lower`` are preserved unchanged (bare and marker)."""
    lower = {"a": 1, "b__extend": ["x"]}
    higher = {"c": 2}
    combined = combine_patches(lower, higher)
    assert combined == {"a": 1, "b__extend": ["x"], "c": 2}


def test_combine_patches_extend_plus_extend_recurses_into_nested_dict() -> None:
    """Dict-valued markers combine recursively, preserving nested markers."""
    lower = {"permissions__extend": {"allow__extend": ["X"]}}
    higher = {"permissions__extend": {"allow__extend": ["Y"], "deny__extend": ["Z"]}}
    combined = combine_patches(lower, higher)
    assert combined == {"permissions__extend": {"allow__extend": ["X", "Y"], "deny__extend": ["Z"]}}


def test_combine_patches_higher_bare_dict_strips_lower_contribution() -> None:
    """A higher *bare* dict value wins and does not merge in the lower marker; its
    own nested markers are kept structured (combined against nothing)."""
    lower = {"permissions__extend": {"allow__extend": ["X"]}}
    higher = {"permissions": {"deny__extend": ["Z"]}}
    combined = combine_patches(lower, higher)
    assert combined == {"permissions": {"deny__extend": ["Z"]}}


def test_combine_patches_lower_bare_dict_with_nested_marker_plus_higher_extend() -> None:
    """Row 2, dict case: a lower *bare* dict that itself carries a nested marker,
    combined with a higher dict marker, must interleave the nested markers (not copy
    the lower marker verbatim). The bare ``f`` slot is kept, and the nested
    ``allow__extend`` markers from both layers accumulate in lower-then-higher order
    so a later fold against a base extends correctly without inverting precedence."""
    lower = {"permissions": {"defaultMode": "acceptEdits", "allow__extend": ["X"]}}
    higher = {"permissions__extend": {"allow__extend": ["Y"]}}
    combined = combine_patches(lower, higher)
    assert combined == {"permissions": {"defaultMode": "acceptEdits", "allow__extend": ["X", "Y"]}}


def _fold(base: dict[str, Any], patch: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The provision fold expressed via the unified primitives: ``merge`` against the
    concrete ``base`` (tracking narrowings) then ``finalize`` (resolving any marker
    preserved against an absent key) -- matching ``_build_settings_json``."""
    merged, narrowings = merge(base, patch)
    return finalize(merged), narrowings


@pytest.mark.parametrize(
    ("base", "lower", "higher"),
    [
        # Row 1: extend + extend accumulate onto a present base value.
        ({"f": ["V"]}, {"f__extend": ["A"]}, {"f__extend": ["B"]}),
        # Row 2: lower bare + higher extend.
        ({"f": ["V"]}, {"f": ["A"]}, {"f__extend": ["B"]}),
        # Row 3: higher bare wipes lower extend.
        ({"f": ["V"]}, {"f__extend": ["A"]}, {"f": ["B"]}),
        # Row 4: higher bare wipes lower bare.
        ({"f": ["V"]}, {"f": ["A"]}, {"f": ["B"]}),
        # Nested dict recursion against a present base.
        (
            {"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}},
            {"permissions__extend": {"allow__extend": ["X"]}},
            {"permissions__extend": {"allow__extend": ["Y"]}},
        ),
        # Disjoint keys across layers (no whole-dict clobber).
        ({"p": {"k": 1}}, {"a__extend": ["x"]}, {"b__extend": ["y"]}),
        # Lower *bare* dict carrying a nested marker + higher dict marker: the nested
        # markers must interleave (lower then higher), not let the lower marker
        # resolve last and override the higher contribution.
        (
            {"permissions": {"allow": ["base"]}},
            {"permissions": {"defaultMode": "acceptEdits", "allow__extend": ["git"]}},
            {"permissions__extend": {"allow__extend": ["npm"]}},
        ),
        # Value-level precedence: a lower bare dict and a higher marker both touch the
        # same nested key; the higher layer must win at that key.
        (
            {"a": "base"},
            {"a": {"c__extend": {"b": "lower"}}},
            {"a__extend": {"c__extend": {"b": "higher"}}},
        ),
        # __assign in the mix: a higher __assign over a lower extend.
        ({"f": ["V"]}, {"f__extend": ["A"]}, {"f__assign": ["B"]}),
    ],
)
def test_merge_is_associative_under_finalize(
    base: dict[str, Any], lower: dict[str, Any], higher: dict[str, Any]
) -> None:
    """``finalize(merge(merge(B, X), Y)) == finalize(merge(B, merge(X, Y)))`` for the
    four-rule table plus nested-dict recursion and ``__assign`` -- the core
    associativity guarantee that lets per-scope patches be condensed at config-load
    and applied at provision."""
    left = finalize(merge(merge(base, lower)[0], higher)[0])
    right = finalize(merge(base, merge(lower, higher)[0])[0])
    assert left == right


# =============================================================================
# merge / finalize -- threaded-narrowing provision fold
# =============================================================================


def test_merge_extend_against_present_dict_preserves_siblings() -> None:
    """A nested ``allow__extend`` merges onto the base, preserving ``defaultMode``;
    no narrowing (extend is a superset)."""
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}}
    merged, narrowings = _fold(base, {"permissions__extend": {"allow__extend": ["X"]}})
    assert merged == {"permissions": {"defaultMode": "acceptEdits", "allow": ["old", "X"]}}
    assert narrowings == []


def test_merge_top_level_bare_narrows() -> None:
    """A bare key that drops a non-empty aggregate from the base is recorded."""
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits"}}
    merged, narrowings = _fold(base, {"permissions": {"allow": ["X"]}})
    assert merged == {"permissions": {"allow": ["X"]}}
    assert narrowings == ["permissions"]


def test_merge_nested_bare_inside_extend_narrows() -> None:
    """The known-gap fix: a bare key nested inside an ``__extend`` value that drops a
    non-empty base aggregate is recorded at its dotted path (previously unchecked)."""
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}}
    merged, narrowings = _fold(base, {"permissions__extend": {"allow": ["X"]}})
    # defaultMode survives the outer extend; allow is replaced (dropping "old").
    assert merged == {"permissions": {"defaultMode": "acceptEdits", "allow": ["X"]}}
    assert narrowings == ["permissions.allow"]


def test_merge_assigns_absent_key_without_narrowing() -> None:
    """Assigning a brand-new key (no base value) never narrows."""
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits"}}
    merged, narrowings = _fold(base, {"model": "opus"})
    assert merged == {"permissions": {"defaultMode": "acceptEdits"}, "model": "opus"}
    assert narrowings == []


def test_merge_resolves_nested_markers_in_bare_dict() -> None:
    """A bare dict value carrying its own ``__extend`` resolves against empty
    (extend-against-nothing = assign), leaving no marker in the output."""
    base: dict[str, Any] = {}
    merged, narrowings = _fold(base, {"permissions": {"allow__extend": ["X"]}})
    assert merged == {"permissions": {"allow": ["X"]}}
    assert narrowings == []


def test_finalize_output_has_no_markers() -> None:
    """Against a concrete base every marker resolves; the output has no ``__extend``."""
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}}
    merged, _ = _fold(base, {"permissions__extend": {"allow__extend": ["X"]}})

    def _has_marker(value: Any) -> bool:
        if isinstance(value, dict):
            return any(is_extend_key(k) for k in value) or any(_has_marker(v) for v in value.values())
        if isinstance(value, list):
            return any(_has_marker(v) for v in value)
        return False

    assert not _has_marker(merged)


def test_merge_against_concrete_base_preserves_untouched_siblings() -> None:
    """``merge`` carries through base keys the patch does not mention (so no separate
    overlay is needed in ``_build_settings_json``)."""
    base: dict[str, Any] = {"model": "opus", "permissions": {"allow": ["old"]}}
    merged, narrowings = _fold(base, {"permissions__extend": {"allow__extend": ["X"]}})
    assert merged == {"model": "opus", "permissions": {"allow": ["old", "X"]}}
    assert narrowings == []


def test_merge_static_override_does_not_narrow() -> None:
    """A ``StaticList`` override that drops base entries is a value-set, not narrowing."""
    base: dict[str, Any] = {"cli_args": ["--debug", "--trace"]}
    merged, narrowings = _fold(base, {"cli_args": StaticList(["--verbose"])})
    assert merged == {"cli_args": ["--verbose"]}
    assert narrowings == []


def test_merge_assign_suppresses_narrowing_but_bare_does_not() -> None:
    """``__assign`` over a non-empty base aggregate suppresses the narrowing the same
    bare assign would record."""
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}}
    _, bare_narrowings = _fold(base, {"permissions": {"allow": ["X"]}})
    assert bare_narrowings == ["permissions"]
    _, assign_narrowings = _fold(base, {"permissions__assign": {"allow": ["X"]}})
    assert assign_narrowings == []


# =============================================================================
# __assign operator
# =============================================================================


def test_is_assign_key_recognises_suffix() -> None:
    assert is_assign_key("permissions__assign")
    assert is_assign_key("a__assign")


def test_is_assign_key_rejects_bare_suffix() -> None:
    assert not is_assign_key(ASSIGN_SUFFIX)


def test_is_assign_key_rejects_plain_field() -> None:
    assert not is_assign_key("permissions")
    assert not is_assign_key("permissions__extend")


def test_assign_bare_key_strips_suffix() -> None:
    assert assign_bare_key("permissions__assign") == "permissions"


def test_fold_assign_key_assigns_like_bare_but_records_no_narrowing() -> None:
    """``key__assign`` replaces the base value identically to a bare key, but the
    narrowing that a bare key would record is suppressed."""
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}}
    merged, narrowings = _fold(base, {"permissions__assign": {"allow": ["X"]}})
    assert merged == {"permissions": {"allow": ["X"]}}
    assert narrowings == []


def test_fold_bare_key_still_narrows_when_assign_would_not() -> None:
    """Sanity contrast: the same drop via a *bare* key still records a narrowing."""
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}}
    _, narrowings = _fold(base, {"permissions": {"allow": ["X"]}})
    assert narrowings == ["permissions"]


def test_fold_assign_then_extend_in_same_layer_resets_without_warning_then_adds() -> None:
    """``key__assign`` (assign-phase) runs before ``key__extend`` (extend-phase):
    reset-without-warning, then add."""
    base: dict[str, Any] = {"unset_vars": ["OLD"]}
    merged, narrowings = _fold(base, {"unset_vars__assign": [], "unset_vars__extend": ["A"]})
    assert merged == {"unset_vars": ["A"]}
    assert narrowings == []


def test_fold_assign_nested_inside_extend_suppresses_nested_narrowing() -> None:
    """A bare key nested in an ``__extend`` narrows; switching it to ``__assign``
    suppresses that nested narrowing while keeping the same merged value."""
    base: dict[str, Any] = {"permissions": {"defaultMode": "acceptEdits", "allow": ["old"]}}
    merged, narrowings = _fold(base, {"permissions__extend": {"allow__assign": ["X"]}})
    assert merged == {"permissions": {"defaultMode": "acceptEdits", "allow": ["X"]}}
    assert narrowings == []


def test_fold_bare_plus_assign_same_key_raises() -> None:
    """A bare key and ``key__assign`` for the same field in one layer is a
    contradictory double-assign -> ``ConfigParseError``."""
    with pytest.raises(ConfigParseError, match="Conflicting assignment"):
        _fold({}, {"model": "opus", "model__assign": "sonnet"})


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


def test_combine_patches_higher_assign_wins_and_keeps_suffix() -> None:
    """A higher ``__assign`` wins over a lower marker (like bare) and keeps its
    suffix so the eventual fold suppresses narrowing."""
    lower: dict[str, Any] = {"f__extend": ["A"]}
    higher: dict[str, Any] = {"f__assign": ["B"]}
    combined = combine_patches(lower, higher)
    assert combined == {"f__assign": ["B"]}


def test_combine_patches_lower_assign_plus_higher_extend_keeps_assign_suffix() -> None:
    """A lower ``__assign`` extended by a higher ``__extend`` extends the assigned
    value and retains the ``__assign`` suffix (no-warn intent preserved)."""
    lower: dict[str, Any] = {"f__assign": ["A"]}
    higher: dict[str, Any] = {"f__extend": ["B"]}
    combined = combine_patches(lower, higher)
    assert combined == {"f__assign": ["A", "B"]}


def test_combine_patches_bare_plus_assign_same_layer_raises() -> None:
    with pytest.raises(ConfigParseError, match="Conflicting assignment"):
        combine_patches({}, {"f": 1, "f__assign": 2})
