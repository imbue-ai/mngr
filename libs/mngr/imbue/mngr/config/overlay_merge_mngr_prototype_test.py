"""Property test for the top-level ``MngrConfig`` overlay-merge PROTOTYPE.

Ground truth: ``merge_mngr_config_via_overlay(base, override)`` must equal
``base.merge_with(override)`` for every ``(base, override)`` pair, compared at the
user-visible stage (after the loader's final default-applying validation, via
``finalize_like_loader``). The prototype reproduces the result purely through
dump -> overlay -> reparse and never calls ``merge_with``, so the equality is a
real check, not a tautology (guarded explicitly by
``test_prototype_does_not_call_merge_with``).

Construction mirrors the loader exactly: the **base** is the loader's defaulted
accumulator (an initial ``model_construct`` config, optionally pre-merged with
prior layers via the production ``merge_with`` -- this is what the left operand of
every real merge looks like, with non-``None`` ``retry`` / ``logging`` and
defaulted scalars), and the **override** is a fresh ``parse_config`` layer (the
padded sparse construction whose ``None`` scalars are the whole point of the
top-level probe). Container entries that need a subclass or a settings patch are
produced by registering a settings-bearing ``AgentTypeConfig`` subclass as the
``claude`` agent type and a base provider config as the ``docker`` backend, so the
real ``parse_config`` path builds them.

The corpus spans every field kind in the spec: scalars set/unset/overlapping
(including non-default-in-base / unset-in-override and vice versa); aggregate
assign-by-default fields (``unset_vars``, ``enabled_backends``,
``work_dir_extra_paths``, ``pre_command_scripts``); the five container-additive
dicts overlapping / disjoint / empty, with nested ``settings_overrides``
accumulation (bare, ``__extend``, nested ``__extend``) inside ``agent_types``
entries; ``retry`` / ``logging`` set/unset/both; and mixtures.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated
from typing import Any

import pytest
from pydantic import Field

from imbue.mngr.config.agent_config_registry import register_agent_config
from imbue.mngr.config.agent_config_registry import reset_agent_config_registry
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.config.data_types import SettingsPatchField
from imbue.mngr.config.overlay_merge_mngr_prototype import finalize_like_loader
from imbue.mngr.config.overlay_merge_mngr_prototype import merge_mngr_config_via_overlay
from imbue.mngr.config.overlay_merge_mngr_prototype import parse_layer
from imbue.mngr.config.provider_config_registry import register_provider_config
from imbue.mngr.config.provider_config_registry import reset_provider_config_registry
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.utils.logging import LoggingConfig


class _ClaudeLikeConfig(AgentTypeConfig):
    """A settings-bearing ``AgentTypeConfig`` subclass standing in for the real
    ``ClaudeAgentConfig``: it carries a ``SettingsPatchField`` (the accumulate-not-
    assign field the container merge must reproduce) plus a subclass-only scalar, so
    the corpus exercises both the settings-patch combine and subclass round-tripping
    inside ``agent_types`` entries without depending on the claude plugin package.
    """

    settings_overrides: Annotated[dict[str, Any], SettingsPatchField()] = Field(default_factory=dict)
    auto_dismiss_dialogs: bool | None = Field(default=None)


@pytest.fixture(autouse=True)
def _registered_config_classes() -> Iterator[None]:
    """Register the stand-in container-entry config classes for the duration of each
    test, then reset the registries (test isolation). ``claude`` -> the settings-
    bearing subclass; ``docker`` -> the base provider config (so provider blocks
    parse). Unregistered plugin / command / create-template entries fall back to
    their base classes, which need no registration.
    """
    reset_agent_config_registry()
    reset_provider_config_registry()
    register_agent_config("claude", _ClaudeLikeConfig)
    register_provider_config("docker", ProviderInstanceConfig)
    try:
        yield
    finally:
        reset_agent_config_registry()
        reset_provider_config_registry()


def _initial_config() -> MngrConfig:
    """The loader's initial accumulator config: ``model_construct`` with defaults
    applied (so ``retry`` / ``logging`` are non-``None`` and scalars are defaulted),
    exactly as ``load_config`` builds its starting point before merging layers."""
    return MngrConfig.model_construct(
        prefix="mngr-",
        default_host_dir=Path("~/.mngr"),
        agent_types={},
        providers={},
        plugins={},
        logging=LoggingConfig(),
        commands={},
    )


def _base_from_layers(*raw_layers: dict[str, Any]) -> MngrConfig:
    """Build a base accumulator by merging ``raw_layers`` (each a raw TOML-shaped
    dict, parsed via the padded ``parse_config``) into the initial config using the
    production ``merge_with`` -- the genuine left-operand shape of a real merge."""
    config = _initial_config()
    for raw in raw_layers:
        config = config.merge_with(parse_layer(raw))
    return config


# Each case is (label, base_layers, override_raw). ``base_layers`` are raw
# TOML-shaped dicts merged into the initial config (via the production
# ``merge_with``) to form the base accumulator; ``override_raw`` is parsed into a
# padded sparse layer. Both are built at *test* time (inside the parametrized
# function) so the registry fixture has already registered ``claude`` / ``docker``;
# building them at import time would parse against an empty registry.
_CASES: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = [
    # --- scalars: set/unset/overlap, default vs non-default ---
    ("scalar_override_only", [], {"prefix": "ovr-"}),
    ("scalar_base_only", [{"prefix": "base-"}], {"headless": True}),
    ("both_empty", [], {}),
    ("scalar_overlap", [{"prefix": "base-"}], {"prefix": "ovr-"}),
    (
        "nondefault_base_unset_override",
        [{"agent_ready_timeout": 99.0, "is_error_reporting_enabled": False}],
        {"prefix": "y"},
    ),
    ("override_sets_default_value", [{"is_error_reporting_enabled": False}], {"is_error_reporting_enabled": True}),
    ("strnone_override", [{"pager": "less", "connect_command": "cc"}], {"pager": "more"}),
    ("strnone_carry_base", [{"pager": "less", "connect_command": "cc"}], {"prefix": "w"}),
    # --- aggregate assign-by-default fields (not container) ---
    ("unset_vars_replace", [{"unset_vars": ["A", "B"]}], {"unset_vars": ["C"]}),
    ("unset_vars_carry_base", [{"unset_vars": ["A", "B"]}], {"prefix": "x"}),
    ("enabled_backends_replace", [{"enabled_backends": ["docker"]}], {"enabled_backends": ["modal"]}),
    ("work_dir_extra_paths", [], {"work_dir_extra_paths": {"/a": "COPY"}}),
    ("pre_command_scripts", [], {"pre_command_scripts": {"create": ["echo hi"]}}),
    # --- retry / logging sub-models ---
    ("retry_partial", [], {"retry": {"connect_retry_times": 9}}),
    ("retry_carry_base", [{"retry": {"connect_retry_times": 7}}], {"prefix": "x"}),
    ("retry_both_set", [{"retry": {"connect_retry_times": 7}}], {"retry": {"connect_retry_delay": "9s"}}),
    ("logging_partial", [{"logging": {"max_log_size_mb": 7}}], {"logging": {"is_logging_env_vars": True}}),
    ("logging_carry_base", [{"logging": {"max_log_size_mb": 7}}], {"prefix": "z"}),
    # --- agent_types: disjoint / shared / subclass / cli_args ---
    ("agent_types_disjoint", [{"agent_types": {"foo": {"command": "c"}}}], {"agent_types": {"bar": {"command": "d"}}}),
    (
        "agent_types_shared",
        [{"agent_types": {"foo": {"command": "c"}}}],
        {"agent_types": {"foo": {"cli_args": "--x"}}},
    ),
    (
        "agent_types_subclass_scalar",
        [{"agent_types": {"c": {"parent_type": "claude", "auto_dismiss_dialogs": True}}}],
        {"agent_types": {"c": {"parent_type": "claude", "cli_args": "--y"}}},
    ),
    (
        "agent_types_cli_args_string_replaces_list",
        [{"agent_types": {"a": {"cli_args": ["x", "y"]}}}],
        {"agent_types": {"a": {"cli_args": "--new flag"}}},
    ),
    # --- agent_types: nested settings_overrides accumulation ---
    (
        "settings_bare_disjoint",
        [{"agent_types": {"c": {"parent_type": "claude", "settings_overrides": {"model": "sonnet"}}}}],
        {"agent_types": {"c": {"parent_type": "claude", "settings_overrides": {"env": "x"}}}},
    ),
    (
        "settings_bare_overlap",
        [{"agent_types": {"c": {"parent_type": "claude", "settings_overrides": {"model": "sonnet", "k": 1}}}}],
        {"agent_types": {"c": {"parent_type": "claude", "settings_overrides": {"model": "opus"}}}},
    ),
    (
        "settings_extend_accumulate",
        [
            {
                "agent_types": {
                    "c": {
                        "parent_type": "claude",
                        "settings_overrides": {"permissions__extend": {"allow__extend": ["Bash(a)"]}},
                    }
                }
            }
        ],
        {
            "agent_types": {
                "c": {
                    "parent_type": "claude",
                    "settings_overrides": {"permissions__extend": {"allow__extend": ["Bash(b)"]}},
                }
            }
        },
    ),
    (
        "settings_nested_extend_plus_bare",
        [
            {
                "agent_types": {
                    "c": {
                        "parent_type": "claude",
                        "settings_overrides": {
                            "model": "sonnet",
                            "permissions__extend": {"allow__extend": ["Bash(a)"]},
                        },
                    }
                }
            }
        ],
        {
            "agent_types": {
                "c": {
                    "parent_type": "claude",
                    "settings_overrides": {"env": "x", "permissions__extend": {"allow__extend": ["Bash(b)"]}},
                }
            }
        },
    ),
    # --- commands: defaults assign vs default_subcommand-only ---
    (
        "commands_subcommand_only",
        [{"commands": {"create": {"new_host": "docker", "default_subcommand": "x"}}}],
        {"commands": {"create": {"default_subcommand": "y"}}},
    ),
    (
        "commands_defaults_replace",
        [{"commands": {"create": {"new_host": "docker"}}}],
        {"commands": {"create": {"connect": False}}},
    ),
    (
        "commands_disjoint",
        [{"commands": {"create": {"new_host": "docker"}}}],
        {"commands": {"start": {"foreground": True}}},
    ),
    # --- providers ---
    (
        "providers_partial",
        [{"providers": {"p": {"backend": "docker", "is_enabled": True}}}],
        {"providers": {"p": {"backend": "docker", "min_online_host_age_seconds": 5.0}}},
    ),
    (
        "providers_disjoint",
        [{"providers": {"p": {"backend": "docker"}}}],
        {"providers": {"q": {"backend": "docker", "is_enabled": False}}},
    ),
    # --- plugins ---
    ("plugins_partial", [{"plugins": {"pl": {"enabled": True}}}], {"plugins": {"pl": {"enabled": False}}}),
    ("plugins_disjoint", [{"plugins": {"pl": {"enabled": True}}}], {"plugins": {"other": {"enabled": False}}}),
    # --- create_templates ---
    (
        "create_templates_replace",
        [{"create_templates": {"t": {"new_host": "modal"}}}],
        {"create_templates": {"t": {"target_path": "/r"}}},
    ),
    (
        "create_templates_disjoint",
        [{"create_templates": {"t": {"new_host": "modal"}}}],
        {"create_templates": {"u": {"new_host": "docker"}}},
    ),
    # --- mixtures: scalars + multiple containers + retry together ---
    (
        "combined_all_kinds",
        [
            {
                "prefix": "base-",
                "agent_ready_timeout": 42.0,
                "agent_types": {
                    "c": {
                        "parent_type": "claude",
                        "auto_dismiss_dialogs": True,
                        "settings_overrides": {
                            "model": "sonnet",
                            "permissions__extend": {"allow__extend": ["Bash(a)"]},
                        },
                    },
                    "keep": {"command": "kept"},
                },
                "commands": {"create": {"new_host": "docker"}},
                "retry": {"connect_retry_times": 7},
            }
        ],
        {
            "prefix": "ovr-",
            "agent_types": {
                "c": {
                    "parent_type": "claude",
                    "cli_args": "--c",
                    "settings_overrides": {"env": "x", "permissions__extend": {"allow__extend": ["Bash(b)"]}},
                },
                "new": {"command": "added"},
            },
            "commands": {"start": {"foreground": True}},
            "logging": {"max_log_size_mb": 3},
        },
    ),
]


@pytest.mark.parametrize(
    "base_layers,override_raw", [pytest.param(layers, o, id=label) for label, layers, o in _CASES]
)
def test_overlay_mngr_prototype_matches_merge_with(
    base_layers: list[dict[str, Any]], override_raw: dict[str, Any]
) -> None:
    """The overlay pipeline reproduces top-level ``MngrConfig.merge_with`` exactly,
    compared at the user-visible (final-default-applied) stage on both sides."""
    base = _base_from_layers(*base_layers)
    override = parse_layer(override_raw)
    expected = finalize_like_loader(base.merge_with(override))
    actual = finalize_like_loader(merge_mngr_config_via_overlay(base, override))
    assert actual == expected


def test_container_entry_subclass_is_preserved() -> None:
    """A ``claude`` (subclass) ``agent_types`` entry round-trips as the subclass
    through the pipeline, so subclass-only fields survive (the
    ``_merge_container_dict`` class-handling reproduction)."""
    base = _base_from_layers({"agent_types": {"c": {"parent_type": "claude", "auto_dismiss_dialogs": True}}})
    override = parse_layer({"agent_types": {"c": {"parent_type": "claude", "cli_args": "--y"}}})
    actual = merge_mngr_config_via_overlay(base, override)
    entry = actual.agent_types[AgentTypeName("c")]
    assert type(entry) is _ClaudeLikeConfig
    assert entry.auto_dismiss_dialogs is True


def test_prototype_does_not_call_merge_with() -> None:
    """Guard against the property test becoming tautological: the prototype's
    executable code must not call ``merge_with``.

    Docstrings and ``#`` comments legitimately mention ``merge_with`` (the thing
    being reproduced and explained), so both are stripped before the check -- only
    real code is inspected. ``parse_config`` (which the prototype imports for
    ``parse_layer``) does not call ``merge_with`` itself, so importing it is not a
    back-door.
    """
    source = Path(__file__).with_name("overlay_merge_mngr_prototype.py").read_text()
    # Even-indexed split parts are code (outside docstrings); odd-indexed are docstrings.
    code_outside_docstrings = "".join(part for index, part in enumerate(source.split('"""')) if index % 2 == 0)
    # Strip ``#`` line comments too, so a comment documenting the reproduced method
    # is not mistaken for a call.
    code_only = "\n".join(line.split("#", 1)[0] for line in code_outside_docstrings.splitlines())
    assert "merge_with" not in code_only
