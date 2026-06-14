"""Property test for the ``parent_type`` inheritance overlay-merge PROTOTYPE.

Ground truth: ``apply_custom_overrides_via_overlay(parent, custom)`` must equal
``_apply_custom_overrides_to_parent_config(parent, custom)`` for every
``(parent, custom)`` pair. The prototype reproduces the result purely through
``model_dump`` -> overlay -> ``model_validate`` and never calls the production
function (nor ``merge`` / ``merge_with`` / ``combine_patches``), so the equality is
a real check, not a tautology (guarded by ``test_prototype_does_not_call_*``).

The pairs are constructed the way ``resolve_agent_type`` builds them: the
``parent`` is either ``config_class()`` (bare defaults, the
``parent_user_config is None`` branch) or ``config_class()`` already folded with a
parent user config (the two-call ``parent_base_config`` branch, built here through
the *production* ``_apply_custom_overrides_to_parent_config`` so the left operand is
the genuine shape a real resolve produces). The ``custom`` is a sparse
``model_construct`` config with ``parent_type`` set (often plus ``plugin``), exactly
the ``[agent_types.X]`` block the loader parses.

The corpus spans every axis the spec calls out: the child sets disjoint / overlapping
/ no fields; ``settings_overrides`` bare / ``__extend`` / nested / ``__assign`` /
accumulating across the parent+child boundary; subclass-only fields
(``ClaudeAgentConfig.auto_dismiss_dialogs`` etc.) set on the child and/or parent;
``_METADATA_FIELDS`` (``parent_type`` / ``plugin``) present on the child (must be
ignored); and a ``ClaudeAgentConfig`` parent so the class-switching crux is exercised
(the output must be a ``ClaudeAgentConfig``).
"""

from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.config.agent_config_registry import _apply_custom_overrides_to_parent_config
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.loader import _normalize_tuple_fields_for_construct
from imbue.mngr.config.overlay_merge_parent_type_prototype import apply_custom_overrides_via_overlay
from imbue.mngr_claude.plugin import ClaudeAgentConfig


def _agent(**fields: Any) -> AgentTypeConfig:
    """Build a sparse base ``AgentTypeConfig`` the loader way: ``model_construct``
    over only the written keys (so ``model_fields_set`` is faithful), with the same
    tuple/string normalization the loader applies before construct."""
    return AgentTypeConfig.model_construct(**_normalize_tuple_fields_for_construct(fields))


def _claude(**fields: Any) -> ClaudeAgentConfig:
    """Build a sparse ``ClaudeAgentConfig`` the loader way (see ``_agent``)."""
    return ClaudeAgentConfig.model_construct(**_normalize_tuple_fields_for_construct(fields))


# Each case is ``(label, parent, custom)``. ``parent`` plays the role
# ``resolve_agent_type`` gives it: either a bare ``config_class()`` or a config
# already folded with a parent user block. ``custom`` is the child ``[agent_types.X]``
# block, always carrying ``parent_type`` (and often ``plugin``) so the
# ``_METADATA_FIELDS`` skip is exercised.
_CASES: list[tuple[str, AgentTypeConfig, AgentTypeConfig]] = [
    # --- the parent_user_config is None branch: bare defaults parent ---
    ("bare_parent_child_scalar", AgentTypeConfig(), _agent(parent_type="claude", command="c")),
    (
        "bare_claude_parent_child_scalar",
        ClaudeAgentConfig(),
        _claude(parent_type="claude", command="my-claude"),
    ),
    # --- child sets only metadata: must return parent unchanged ---
    ("child_only_metadata", ClaudeAgentConfig(), _claude(parent_type="claude")),
    ("child_only_metadata_plus_plugin", ClaudeAgentConfig(), _claude(parent_type="claude", plugin="claude")),
    (
        "child_only_metadata_nonbare_parent",
        ClaudeAgentConfig(auto_dismiss_dialogs=True, cli_args=("a",)),
        _claude(parent_type="claude"),
    ),
    # --- metadata present on child alongside real fields (metadata ignored) ---
    (
        "child_metadata_and_fields",
        ClaudeAgentConfig(),
        _claude(parent_type="claude", plugin="claude", cli_args=("x",), auto_dismiss_dialogs=True),
    ),
    # --- scalar fields: disjoint / overlapping ---
    (
        "scalars_disjoint",
        _claude(command="base-cmd"),
        _claude(parent_type="claude", cli_args=("a",)),
    ),
    (
        "scalars_overlap",
        _claude(command="base-cmd", version="1.0.0"),
        _claude(parent_type="claude", command="ovr-cmd"),
    ),
    # --- subclass-only fields on child and/or parent ---
    (
        "subclass_field_on_child",
        ClaudeAgentConfig(),
        _claude(parent_type="claude", auto_dismiss_dialogs=True),
    ),
    (
        "subclass_field_on_parent_only",
        ClaudeAgentConfig(auto_dismiss_dialogs=True),
        _claude(parent_type="claude", cli_args=("x",)),
    ),
    (
        "subclass_field_on_both",
        ClaudeAgentConfig(auto_dismiss_dialogs=True, auto_allow_permissions=True),
        _claude(parent_type="claude", auto_dismiss_dialogs=False),
    ),
    (
        "subclass_version_overlap",
        ClaudeAgentConfig(version="1.0.0"),
        _claude(parent_type="claude", version="2.0.0"),
    ),
    # --- other tuple/aggregate fields: assign-by-default ---
    (
        "env_assign",
        _claude(env=("A=1", "B=2")),
        _claude(parent_type="claude", env=("C=3",)),
    ),
    (
        "env_base_carry",
        _claude(env=("A=1",)),
        _claude(parent_type="claude", create_directory=("/tmp",)),
    ),
    # --- settings_overrides: bare keys, accumulation across parent/child ---
    (
        "settings_bare_disjoint",
        _claude(settings_overrides={"model": "sonnet"}),
        _claude(parent_type="claude", settings_overrides={"env": "x"}),
    ),
    (
        "settings_bare_overlap",
        _claude(settings_overrides={"model": "sonnet", "k": 1}),
        _claude(parent_type="claude", settings_overrides={"model": "opus"}),
    ),
    (
        "settings_parent_only",
        _claude(settings_overrides={"model": "sonnet"}),
        _claude(parent_type="claude", cli_args=("x",)),
    ),
    (
        "settings_child_only",
        ClaudeAgentConfig(),
        _claude(parent_type="claude", settings_overrides={"model": "opus"}),
    ),
    # --- settings_overrides: __extend accumulation across the boundary ---
    (
        "settings_extend_accumulate",
        _claude(settings_overrides={"permissions__extend": {"allow__extend": ["Bash(a)"]}}),
        _claude(
            parent_type="claude",
            settings_overrides={"permissions__extend": {"allow__extend": ["Bash(b)"]}},
        ),
    ),
    (
        "settings_nested_extend",
        _claude(settings_overrides={"model": "sonnet", "permissions__extend": {"allow__extend": ["Bash(a)"]}}),
        _claude(
            parent_type="claude",
            settings_overrides={"env": "x", "permissions__extend": {"allow__extend": ["Bash(b)"]}},
        ),
    ),
    (
        "settings_extend_over_bare",
        _claude(settings_overrides={"permissions": {"allow": ["x"]}}),
        _claude(parent_type="claude", settings_overrides={"permissions__extend": {"allow__extend": ["y"]}}),
    ),
    (
        "settings_bare_over_extend",
        _claude(settings_overrides={"permissions__extend": {"allow": ["x"]}}),
        _claude(parent_type="claude", settings_overrides={"permissions": {"allow": ["y"]}}),
    ),
    # --- settings_overrides: __assign ---
    (
        "settings_assign_marker",
        _claude(settings_overrides={"model": "sonnet"}),
        _claude(parent_type="claude", settings_overrides={"model__assign": "opus"}),
    ),
    (
        "settings_assign_then_extend",
        _claude(settings_overrides={"hooks__extend": {"a": [1]}}),
        _claude(
            parent_type="claude",
            settings_overrides={"hooks__assign": {"b": [2]}, "hooks__extend": {"c": [3]}},
        ),
    ),
    # --- child sets settings to empty dict over a non-empty parent (combine no-op) ---
    (
        "settings_child_empty",
        _claude(settings_overrides={"model": "sonnet"}),
        _claude(parent_type="claude", settings_overrides={}),
    ),
    # --- combined: subclass + settings + scalars together ---
    (
        "combined_all_kinds",
        _claude(
            auto_dismiss_dialogs=True,
            cli_args="--base arg",
            settings_overrides={"model": "sonnet", "permissions__extend": {"allow__extend": ["Bash(a)"]}},
        ),
        _claude(
            parent_type="claude",
            plugin="claude",
            cli_args=("c",),
            version="2.0.0",
            settings_overrides={"env": "x", "permissions__extend": {"allow__extend": ["Bash(b)"]}},
        ),
    ),
    # --- base-class parent (no subclass fields), base-class child ---
    (
        "base_class_parent_and_child",
        _agent(command="base"),
        _agent(parent_type="some-parent", cli_args=("a",)),
    ),
]


@pytest.mark.parametrize("parent,custom", [pytest.param(p, c, id=label) for label, p, c in _CASES])
def test_overlay_prototype_matches_apply_custom_overrides(
    parent: AgentTypeConfig,
    custom: AgentTypeConfig,
) -> None:
    """The overlay pipeline reproduces ``_apply_custom_overrides_to_parent_config``
    exactly, value-for-value, and the output is the *parent's* concrete class."""
    expected = _apply_custom_overrides_to_parent_config(parent, custom)
    actual = apply_custom_overrides_via_overlay(parent, custom)
    assert actual == expected
    # Class-switching: the result follows the parent's concrete class, regardless of
    # the child's class.
    assert type(actual) is type(expected)
    assert type(actual) is type(parent)


def test_two_call_resolve_pattern() -> None:
    """Reproduce ``resolve_agent_type``'s two-call sequence end to end: fold a parent
    user block onto bare defaults, then fold the child onto that. Each call goes
    through the overlay pipeline and must match the production two-call result.

    This is the real shape a custom type resolves through when ``[agent_types.claude]``
    carries its own settings and a child type inherits from it.
    """
    parent_user_config = _claude(auto_dismiss_dialogs=True, settings_overrides={"model": "sonnet"})
    custom_config = _claude(
        parent_type="claude",
        plugin="claude",
        cli_args=("--child",),
        settings_overrides={"permissions__extend": {"allow__extend": ["Bash(npm *)"]}},
    )

    # Production two-call path.
    expected_parent_base = _apply_custom_overrides_to_parent_config(ClaudeAgentConfig(), parent_user_config)
    expected = _apply_custom_overrides_to_parent_config(expected_parent_base, custom_config)

    # Overlay two-call path.
    actual_parent_base = apply_custom_overrides_via_overlay(ClaudeAgentConfig(), parent_user_config)
    actual = apply_custom_overrides_via_overlay(actual_parent_base, custom_config)

    assert actual_parent_base == expected_parent_base
    assert actual == expected
    assert type(actual) is ClaudeAgentConfig


def test_class_switching_base_child_into_claude_parent() -> None:
    """A base ``AgentTypeConfig`` child folded onto a ``ClaudeAgentConfig`` parent must
    yield a ``ClaudeAgentConfig`` -- the subclass-only fields supplied by the parent
    (the child never set them). This is the class-switching crux in isolation."""
    parent = ClaudeAgentConfig(auto_dismiss_dialogs=True, settings_overrides={"model": "sonnet"})
    custom = _agent(parent_type="claude", cli_args=("b",))
    expected = _apply_custom_overrides_to_parent_config(parent, custom)
    actual = apply_custom_overrides_via_overlay(parent, custom)
    assert actual == expected
    assert type(actual) is ClaudeAgentConfig
    # The parent's subclass-only field survives (the child could not have set it).
    assert actual.auto_dismiss_dialogs is True


def test_prototype_does_not_call_production_merge() -> None:
    """Guard against the property test becoming tautological: the prototype's
    executable code must not *call* the production function it reproduces, nor any of
    the field-by-field / patch merge helpers.

    Docstrings and comments legitimately mention these names (the things being
    reproduced, and the lockstep rationale for the ``_METADATA_FIELDS`` import), so
    the check looks for *call* syntax (``name(``) outside triple-quoted strings --
    the prototype reproduces these functions, so naming them in prose is fine but
    invoking them would make the property test a tautology.
    """
    source = Path(__file__).with_name("overlay_merge_parent_type_prototype.py").read_text()
    # Even-indexed split parts are code (outside docstrings); odd-indexed are docstrings.
    code_only = "".join(part for index, part in enumerate(source.split('"""')) if index % 2 == 0)
    # ``merge`` is also a substring of legitimate identifiers imported by the
    # prototype (``node_merge``, ``overlay_merge_*``), so the call-syntax check
    # (``merge(``) is what distinguishes an invocation from a module name.
    for forbidden in (
        "_apply_custom_overrides_to_parent_config(",
        "merge_with(",
        "combine_patches(",
        "merge(",
    ):
        assert forbidden not in code_only, f"prototype must not call {forbidden.rstrip('(')}"
