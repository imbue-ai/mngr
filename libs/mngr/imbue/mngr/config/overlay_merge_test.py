"""Equivalence guard for the production overlay-merge wiring of
``AgentTypeConfig.merge_with``.

``AgentTypeConfig.merge_with`` now computes its result via the overlay node algebra
(``overlay_merge.merge_models_via_overlay``) instead of the old field-by-field
pydantic copy. This test freezes the *old* field-by-field logic verbatim as
``_reference_agent_type_merge`` and asserts the production ``merge_with`` produces
an identical result over a diverse corpus, so the refactor stays a pure
result-preserving change. ``_reference_agent_type_merge`` never calls
``merge_with``, so the equality is a real check, not a tautology.

Test instances are constructed the way the loader builds them: via
``model_construct`` with only the keys the layer "wrote", so ``model_fields_set``
is faithful and sparse (exactly what both the old merge and the pipeline's
``exclude_unset`` dump depend on). The corpus spans every field kind called out in
the spec: disjoint / overlapping / empty scalar sets; ``cli_args`` as a string /
list / unset; ``settings_overrides`` with bare keys, ``__extend``, nested
``__extend``, ``__assign``, accumulation, extend-over-bare and bare-over-extend; the
``ClaudeAgentConfig`` subclass fields; and the ``model_fields_set`` edge cases
(empty override, both empty, base-class override into a subclass self).
"""

from typing import Any

import pytest

from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import is_settings_patch_field
from imbue.mngr.config.loader import _normalize_tuple_fields_for_construct
from imbue.mngr.errors import ConfigParseError
from imbue.mngr_claude.plugin import ClaudeAgentConfig
from imbue.overlay.merge import combine_patches


def _reference_agent_type_merge(base: AgentTypeConfig, override: AgentTypeConfig) -> AgentTypeConfig:
    """The OLD ``AgentTypeConfig.merge_with`` body, frozen verbatim as the reference
    "old" side of the equivalence guard.

    This is the exact field-by-field logic the production ``merge_with`` had before
    being rewired onto the overlay algebra. It must stay independent of the new path
    (it does not call ``merge_with``) so the property test below is a genuine
    old == new check.
    """
    if not isinstance(base, type(override)):
        raise ConfigParseError(f"Cannot merge {base.__class__.__name__} with {type(override).__name__}")

    explicitly_set = override.model_fields_set
    if not explicitly_set:
        return base

    override_values = override.model_dump()
    base_values = base.model_dump()
    updates: list[tuple[str, Any]] = []
    for field_name in explicitly_set:
        field_info = override.__class__.model_fields.get(field_name)
        if field_info is not None and is_settings_patch_field(field_info.metadata):
            updates.append(
                (field_name, combine_patches(base_values.get(field_name) or {}, override_values[field_name]))
            )
        else:
            updates.append((field_name, override_values[field_name]))
    return base.model_copy_update(*updates)


def _agent(**fields: Any) -> AgentTypeConfig:
    """Build a base ``AgentTypeConfig`` the way the loader does: sparse
    ``model_construct`` (only the written keys land in ``model_fields_set``), with
    the same tuple/string normalization the loader applies before construct so a
    string-written ``cli_args`` becomes a ``StringDerivedTuple``.
    """
    return AgentTypeConfig.model_construct(**_normalize_tuple_fields_for_construct(fields))


def _claude(**fields: Any) -> ClaudeAgentConfig:
    """Build a sparse ``ClaudeAgentConfig`` the loader way (see ``_agent``)."""
    return ClaudeAgentConfig.model_construct(**_normalize_tuple_fields_for_construct(fields))


# Each case is (label, base, override). The label is only for readable test ids.
_CASES: list[tuple[str, AgentTypeConfig, AgentTypeConfig]] = [
    # --- model_fields_set edge cases ---
    ("empty_override", _agent(command="c", cli_args="--foo bar"), _agent()),
    ("empty_base", _agent(), _agent(command="c", cli_args=("x",))),
    ("both_empty", _agent(), _agent()),
    # --- scalar fields: disjoint / overlapping / none ---
    ("scalars_disjoint", _agent(command="base-cmd"), _agent(cli_args=("a",))),
    ("scalars_overlap", _agent(command="base-cmd", plugin="p1"), _agent(command="ovr-cmd")),
    ("scalar_parent_type", _agent(parent_type="claude"), _agent(command="x")),
    ("override_command_none", _agent(command="c"), _agent(command=None)),
    # --- cli_args as string / list / unset ---
    ("cli_args_string_replaces_list", _agent(cli_args=("a", "b")), _agent(cli_args="--new flag")),
    ("cli_args_list_replaces_string", _agent(cli_args="--old"), _agent(cli_args=("c", "d"))),
    ("cli_args_base_only", _agent(cli_args="--keep me"), _agent(env=("A=1",))),
    ("cli_args_unset_both", _agent(command="c"), _agent(plugin="p")),
    # --- other tuple/aggregate fields: assign-by-default ---
    ("env_assign", _agent(env=("A=1", "B=2")), _agent(env=("C=3",))),
    ("env_base_carry", _agent(env=("A=1",)), _agent(create_directory=("/tmp",))),
    (
        "many_tuple_fields",
        _agent(extra_provision_command=("p1",), upload_file=("l:r",), env_file=("/e",)),
        _agent(extra_provision_command=("p2",)),
    ),
    # --- ClaudeAgentConfig subclass fields ---
    ("claude_subclass_scalar", _claude(auto_dismiss_dialogs=True), _claude(cli_args=("x",))),
    ("claude_subclass_overlap", _claude(version="2.1.0"), _claude(version="2.2.0")),
    ("claude_subclass_bool_flip", _claude(auto_allow_permissions=True), _claude(auto_dismiss_dialogs=True)),
    # --- settings_overrides: bare keys ---
    (
        "settings_bare_disjoint",
        _claude(settings_overrides={"model": "sonnet"}),
        _claude(settings_overrides={"env": "x"}),
    ),
    (
        "settings_bare_overlap",
        _claude(settings_overrides={"model": "sonnet", "k": 1}),
        _claude(settings_overrides={"model": "opus"}),
    ),
    ("settings_base_only", _claude(settings_overrides={"model": "sonnet"}), _claude(cli_args=("x",))),
    ("settings_override_only", _claude(command="claude"), _claude(settings_overrides={"model": "opus"})),
    # --- settings_overrides: __extend (accumulation) ---
    (
        "settings_extend_accumulate",
        _claude(settings_overrides={"permissions__extend": {"allow__extend": ["Bash(a)"]}}),
        _claude(settings_overrides={"permissions__extend": {"allow__extend": ["Bash(b)"]}}),
    ),
    (
        "settings_nested_extend",
        _claude(settings_overrides={"model": "sonnet", "permissions__extend": {"allow__extend": ["Bash(a)"]}}),
        _claude(settings_overrides={"env": "x", "permissions__extend": {"allow__extend": ["Bash(b)"]}}),
    ),
    (
        "settings_extend_over_bare",
        _claude(settings_overrides={"permissions": {"allow": ["x"]}}),
        _claude(settings_overrides={"permissions__extend": {"allow__extend": ["y"]}}),
    ),
    (
        "settings_bare_over_extend",
        _claude(settings_overrides={"permissions__extend": {"allow": ["x"]}}),
        _claude(settings_overrides={"permissions": {"allow": ["y"]}}),
    ),
    # --- settings_overrides: __assign ---
    (
        "settings_assign_marker",
        _claude(settings_overrides={"model": "sonnet"}),
        _claude(settings_overrides={"model__assign": "opus"}),
    ),
    (
        "settings_assign_then_extend",
        _claude(settings_overrides={"hooks__extend": {"a": [1]}}),
        _claude(settings_overrides={"hooks__assign": {"b": [2]}, "hooks__extend": {"c": [3]}}),
    ),
    # --- combined: subclass fields + settings + scalars together ---
    (
        "combined_all_kinds",
        _claude(
            auto_dismiss_dialogs=True,
            cli_args="--base arg",
            settings_overrides={"model": "sonnet", "permissions__extend": {"allow__extend": ["Bash(a)"]}},
        ),
        _claude(
            cli_args=("c",),
            version="2.0.0",
            settings_overrides={"env": "x", "permissions__extend": {"allow__extend": ["Bash(b)"]}},
        ),
    ),
]


@pytest.mark.parametrize("base,override", [pytest.param(b, o, id=label) for label, b, o in _CASES])
def test_merge_with_matches_frozen_reference(base: AgentTypeConfig, override: AgentTypeConfig) -> None:
    """The production (overlay-backed) ``merge_with`` reproduces the frozen
    field-by-field reference exactly, value-for-value and class-for-class.
    """
    expected = _reference_agent_type_merge(base, override)
    actual = base.merge_with(override)
    assert actual == expected
    # The overlay reparse must preserve the concrete class (subclass stays a subclass).
    assert type(actual) is type(expected)


def test_base_class_override_into_subclass_self() -> None:
    """A base ``AgentTypeConfig`` override merged into a ``ClaudeAgentConfig`` self
    (the loader's "secondary file redefines the type without parent_type" case)
    re-parses back into the subclass, matching the frozen reference.
    """
    base = _claude(auto_dismiss_dialogs=True, cli_args=("a",))
    override = _agent(cli_args=("b",))
    expected = _reference_agent_type_merge(base, override)
    actual = base.merge_with(override)
    assert actual == expected
    assert type(actual) is ClaudeAgentConfig
