"""Equivalence guard for the production overlay-merge wiring of
``AgentTypeConfig.merge_with`` and ``MngrConfig.merge_with``.

Both now compute their result via the overlay node algebra
(``overlay_merge.merge_models_via_overlay``). This file freezes each old
field-by-field body verbatim as a ``_reference_*`` function and asserts the
production method reproduces it over a diverse corpus, so the refactor stays a pure
result-preserving change. The first half covers ``AgentTypeConfig``; the second half
(below the ``MngrConfig.merge_with equivalence guard`` banner) covers the top-level
``MngrConfig`` merge -- the container-additive dicts, the None-padding drop, and the
``serialize_as_any`` subclass round-tripping.

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

import inspect
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated
from typing import Any

import pytest
from pydantic import Field

from imbue.mngr.config.agent_config_registry import _METADATA_FIELDS
from imbue.mngr.config.agent_config_registry import _apply_custom_overrides_to_parent_config
from imbue.mngr.config.agent_config_registry import register_agent_config
from imbue.mngr.config.agent_config_registry import reset_agent_config_registry
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import CommandDefaults
from imbue.mngr.config.data_types import CreateTemplate
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.config.data_types import RetryConfig
from imbue.mngr.config.data_types import ScalarStrTuple
from imbue.mngr.config.data_types import SettingsPatchField
from imbue.mngr.config.data_types import _CONTAINER_DICT_FIELDS
from imbue.mngr.config.data_types import detect_settings_narrowing
from imbue.mngr.config.data_types import get_settings_patch_field_names
from imbue.mngr.config.data_types import is_settings_patch_field
from imbue.mngr.config.loader import _normalize_tuple_fields_for_construct
from imbue.mngr.config.loader import parse_config
from imbue.mngr.config.overlay_merge import _is_settings_patch_narrowing
from imbue.mngr.config.overlay_merge import _overlay_all_narrowing_paths
from imbue.mngr.config.provider_config_registry import register_provider_config
from imbue.mngr.config.provider_config_registry import reset_provider_config_registry
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.utils.logging import LoggingConfig
from imbue.mngr_claude.plugin import ClaudeAgentConfig
from imbue.overlay.merge import combine_patches
from imbue.overlay.merge import merge


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


# =============================================================================
# MngrConfig.merge_with equivalence guard
# =============================================================================
#
# ``MngrConfig.merge_with`` now computes its result via the same overlay node
# algebra (``overlay_merge.merge_models_via_overlay`` with the container /
# None-drop extensions) instead of the old field-by-field pydantic copy. The
# section below freezes the *old* body verbatim as ``_reference_mngr_config_merge``
# (with the old ``_assign_scalar`` / ``_merge_container_dict`` helpers inlined,
# since they were deleted when the body was rewired) and asserts the production
# ``merge_with`` produces an identical result over the spec corpus, compared at the
# user-visible stage (after the loader's final default-applying validation, via
# ``finalize_like_loader``). ``_reference_mngr_config_merge`` never calls
# ``MngrConfig.merge_with``, so the equality is a real check, not a tautology
# (guarded by ``test_mngr_reference_does_not_call_mngr_merge_with``).
#
# Construction mirrors the loader exactly: base = the loader's defaulted
# accumulator (an initial ``model_construct`` config, optionally pre-merged with
# prior layers), override = a fresh ``parse_config`` layer (the padded sparse
# construction whose ``None`` scalars are the whole point of the top-level probe).


def _reference_assign_scalar(base_value: Any, override_value: Any) -> Any:
    """Frozen copy of the old ``data_types._assign_scalar``: override wins iff not None."""
    return override_value if override_value is not None else base_value


def _reference_retry_merge(base: RetryConfig, override: RetryConfig) -> RetryConfig:
    """Frozen copy of the old ``RetryConfig.merge_with`` body (deleted in production),
    kept here as the independent "old" reference for the equivalence guard.

    Scalar fields: override wins if not None.
    """
    return RetryConfig(
        connect_retry_times=override.connect_retry_times
        if override.connect_retry_times is not None
        else base.connect_retry_times,
        connect_retry_delay=override.connect_retry_delay
        if override.connect_retry_delay is not None
        else base.connect_retry_delay,
    )


def _reference_logging_merge(base: LoggingConfig, override: LoggingConfig) -> LoggingConfig:
    """Frozen copy of the old ``LoggingConfig.merge_with`` body (deleted in production),
    kept here as the independent "old" reference for the equivalence guard.

    Scalar fields: override wins if not None.
    """
    return LoggingConfig(
        file_level=override.file_level if override.file_level is not None else base.file_level,
        log_dir=override.log_dir if override.log_dir is not None else base.log_dir,
        max_log_size_mb=override.max_log_size_mb if override.max_log_size_mb is not None else base.max_log_size_mb,
        console_level=override.console_level if override.console_level is not None else base.console_level,
        log_file_path=override.log_file_path if override.log_file_path is not None else base.log_file_path,
        is_logging_commands=override.is_logging_commands
        if override.is_logging_commands is not None
        else base.is_logging_commands,
        is_logging_command_output=override.is_logging_command_output
        if override.is_logging_command_output is not None
        else base.is_logging_command_output,
        is_logging_env_vars=override.is_logging_env_vars
        if override.is_logging_env_vars is not None
        else base.is_logging_env_vars,
        event_type=override.event_type if override.event_type is not None else base.event_type,
        event_source=override.event_source if override.event_source is not None else base.event_source,
        enable_paramiko_logging=override.enable_paramiko_logging
        if override.enable_paramiko_logging is not None
        else base.enable_paramiko_logging,
    )


def _reference_provider_merge(
    base: ProviderInstanceConfig, override: ProviderInstanceConfig
) -> ProviderInstanceConfig:
    """Frozen copy of the old ``ProviderInstanceConfig.merge_with`` body (deleted in
    production), kept here as the independent "old" reference for the equivalence guard.

    Uses ``model_fields_set`` so an override only replaces the fields it actually set.
    """
    if not isinstance(override, base.__class__):
        raise ConfigParseError(f"Cannot merge {base.__class__.__name__} with different provider config type")

    explicitly_set = override.model_fields_set
    if not explicitly_set:
        return base
    base_values = base.model_dump()
    override_values = override.model_dump()
    merged_values: dict[str, Any] = dict(base_values)
    for field_name in explicitly_set:
        merged_values[field_name] = override_values[field_name]
    return base.__class__(**merged_values)


def _reference_plugin_merge(base: Any, override: Any) -> Any:
    """Frozen copy of the old ``PluginConfig.merge_with`` body (deleted in production),
    kept here as the independent "old" reference for the equivalence guard.

    Uses ``model_fields_set`` so plugin subclasses that add extra fields get correct
    assign-by-default semantics on those fields too.
    """
    explicitly_set = override.model_fields_set
    if not explicitly_set:
        return base
    override_values = override.model_dump()
    updates: list[tuple[str, Any]] = [(field_name, override_values[field_name]) for field_name in explicitly_set]
    return base.model_copy_update(*updates)


def _reference_command_defaults_merge(base: CommandDefaults, override: CommandDefaults) -> CommandDefaults:
    """Frozen copy of the old ``CommandDefaults.merge_with`` body (deleted in production),
    kept here as the independent "old" reference for the equivalence guard.
    """
    explicitly_set = override.model_fields_set
    if not explicitly_set:
        return base
    merged_defaults = override.defaults if "defaults" in explicitly_set else base.defaults
    merged_default_subcommand = (
        override.default_subcommand if "default_subcommand" in explicitly_set else base.default_subcommand
    )
    return base.__class__(defaults=merged_defaults, default_subcommand=merged_default_subcommand)


def _reference_create_template_merge(base: CreateTemplate, override: CreateTemplate) -> CreateTemplate:
    """Frozen copy of the old ``CreateTemplate.merge_with`` body (deleted in production),
    kept here as the independent "old" reference for the equivalence guard.
    """
    explicitly_set = override.model_fields_set
    if not explicitly_set:
        return base
    merged_options = override.options if "options" in explicitly_set else base.options
    return base.__class__(options=merged_options)


# Per-container-field entry merge: ``agent_types`` entries still merge via the kept
# production ``AgentTypeConfig.merge_with``; the other container entries' old
# ``merge_with`` bodies were deleted, so the reference dispatches to the frozen copies
# above. This keeps the equivalence guard genuinely independent of the deleted code.
_CONTAINER_ENTRY_REFERENCE_MERGE: dict[str, Callable[[Any, Any], Any]] = {
    "agent_types": lambda base_entry, override_entry: base_entry.merge_with(override_entry),
    "providers": _reference_provider_merge,
    "plugins": _reference_plugin_merge,
    "commands": _reference_command_defaults_merge,
    "create_templates": _reference_create_template_merge,
}


def _reference_merge_container_dict(
    base: dict[Any, Any], override: dict[Any, Any], entry_merge: Callable[[Any, Any], Any]
) -> dict[Any, Any]:
    """Frozen copy of the old ``data_types._merge_container_dict``: per-key additive
    merge (key in both -> entry merge via ``entry_merge``; key in one side -> carried
    through). ``entry_merge`` reproduces the entry type's own (now-deleted, except
    ``AgentTypeConfig``) ``merge_with`` so the reference stays independent of production.
    """
    merged: dict[Any, Any] = {}
    for key in set(base.keys()) | set(override.keys()):
        if key in base and key in override:
            merged[key] = entry_merge(base[key], override[key])
        elif key in override:
            merged[key] = override[key]
        else:
            merged[key] = base[key]
    return merged


def _reference_mngr_config_merge(base: MngrConfig, override: MngrConfig) -> MngrConfig:
    """The OLD ``MngrConfig.merge_with`` body, frozen verbatim as the "old" side of the
    equivalence guard (with the deleted ``_assign_scalar`` / ``_merge_container_dict``
    helpers inlined as ``_reference_*``). It must stay independent of the new path (it
    invokes the *container entries'* own ``merge_with`` but never ``MngrConfig``'s) so
    the property test below is a genuine old == new check.
    """
    merged_agent_types = _reference_merge_container_dict(
        base.agent_types, override.agent_types, _CONTAINER_ENTRY_REFERENCE_MERGE["agent_types"]
    )
    merged_providers = _reference_merge_container_dict(
        base.providers, override.providers, _CONTAINER_ENTRY_REFERENCE_MERGE["providers"]
    )
    merged_plugins = _reference_merge_container_dict(
        base.plugins, override.plugins, _CONTAINER_ENTRY_REFERENCE_MERGE["plugins"]
    )
    merged_commands = _reference_merge_container_dict(
        base.commands, override.commands, _CONTAINER_ENTRY_REFERENCE_MERGE["commands"]
    )
    merged_create_templates = _reference_merge_container_dict(
        base.create_templates, override.create_templates, _CONTAINER_ENTRY_REFERENCE_MERGE["create_templates"]
    )

    merged_retry = (
        _reference_retry_merge(base.retry, override.retry)
        if base.retry is not None and override.retry is not None
        else (override.retry if override.retry is not None else base.retry)
    )
    merged_logging = (
        _reference_logging_merge(base.logging, override.logging)
        if base.logging is not None and override.logging is not None
        else (override.logging if override.logging is not None else base.logging)
    )

    return base.__class__(
        prefix=_reference_assign_scalar(base.prefix, override.prefix),
        default_host_dir=_reference_assign_scalar(base.default_host_dir, override.default_host_dir),
        pager=_reference_assign_scalar(base.pager, override.pager),
        unset_vars=override.unset_vars if override.unset_vars is not None else base.unset_vars,
        work_dir_extra_paths=override.work_dir_extra_paths
        if override.work_dir_extra_paths is not None
        else base.work_dir_extra_paths,
        enabled_backends=override.enabled_backends if override.enabled_backends is not None else base.enabled_backends,
        agent_types=merged_agent_types,
        providers=merged_providers,
        plugins=merged_plugins,
        disabled_plugins=override.disabled_plugins if override.disabled_plugins else base.disabled_plugins,
        commands=merged_commands,
        create_templates=merged_create_templates,
        pre_command_scripts=override.pre_command_scripts
        if override.pre_command_scripts is not None
        else base.pre_command_scripts,
        is_remote_agent_installation_allowed=_reference_assign_scalar(
            base.is_remote_agent_installation_allowed,
            override.is_remote_agent_installation_allowed,
        ),
        connect_command=_reference_assign_scalar(base.connect_command, override.connect_command),
        retry=merged_retry,
        logging=merged_logging,
        is_nested_tmux_allowed=_reference_assign_scalar(base.is_nested_tmux_allowed, override.is_nested_tmux_allowed),
        headless=_reference_assign_scalar(base.headless, override.headless),
        is_error_reporting_enabled=_reference_assign_scalar(
            base.is_error_reporting_enabled,
            override.is_error_reporting_enabled,
        ),
        is_allowed_in_pytest=_reference_assign_scalar(base.is_allowed_in_pytest, override.is_allowed_in_pytest),
        default_destroyed_host_persisted_seconds=_reference_assign_scalar(
            base.default_destroyed_host_persisted_seconds,
            override.default_destroyed_host_persisted_seconds,
        ),
        default_min_online_host_age_seconds=_reference_assign_scalar(
            base.default_min_online_host_age_seconds,
            override.default_min_online_host_age_seconds,
        ),
        agent_ready_timeout=_reference_assign_scalar(base.agent_ready_timeout, override.agent_ready_timeout),
        allow_settings_key_assignment_narrowing=_reference_assign_scalar(
            base.allow_settings_key_assignment_narrowing,
            override.allow_settings_key_assignment_narrowing,
        ),
    )


def _finalize_like_loader(config: MngrConfig) -> MngrConfig:
    """Apply the loader's *final* validation step to a (possibly padded) config,
    yielding the user-visible config with defaults filled in.

    Reproduces the tail of ``load_config``: read field *values* off ``config`` (not a
    serialized dump), omit the padded ``None`` scalars / unset ``retry`` / ``logging``
    so ``model_validate`` supplies their defaults, and pass the container dicts and
    explicitly-set sub-models through as live instances (so concrete container-entry
    subclasses keep their subclass-only fields). Applied to *both* sides of the
    equality, so a genuine value divergence in any set field still survives.
    """
    config_dict: dict[str, Any] = {
        field_name: value for field_name, value in dict(config).items() if value is not None
    }
    return MngrConfig.model_validate(config_dict)


def _parse_layer(raw: dict[str, Any]) -> MngrConfig:
    """Parse a raw TOML-shaped dict into a ``MngrConfig`` the way the loader does
    (the padded ``parse_config`` construction), with no plugins disabled -- so the
    corpus is built through the *real* padded path whose ``None`` scalars are the
    point of the top-level probe.
    """
    return parse_config(raw, frozenset())


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
    """Build a base accumulator by merging ``raw_layers`` (each a raw TOML-shaped dict,
    parsed via the padded ``parse_config``) into the initial config using the
    production ``merge_with`` -- the genuine left-operand shape of a real merge."""
    config = _initial_config()
    for raw in raw_layers:
        config = config.merge_with(_parse_layer(raw))
    return config


class _MngrClaudeLikeConfig(AgentTypeConfig):
    """A settings-bearing ``AgentTypeConfig`` subclass standing in for the real
    ``ClaudeAgentConfig``: carries a ``SettingsPatchField`` plus a subclass-only
    scalar, so the corpus exercises the settings-patch combine and subclass
    round-tripping inside ``agent_types`` entries without depending on the claude
    plugin package's parsing.
    """

    settings_overrides: Annotated[dict[str, Any], SettingsPatchField()] = Field(default_factory=dict)
    auto_dismiss_dialogs: bool | None = Field(default=None)


@pytest.fixture
def _registered_mngr_config_classes() -> Iterator[None]:
    """Register the stand-in container-entry config classes for the duration of each
    test, then reset the registries (test isolation). ``claude`` -> the settings-
    bearing subclass; ``docker`` -> the base provider config (so provider blocks
    parse). Unregistered plugin / command / create-template entries fall back to
    their base classes, which need no registration.
    """
    reset_agent_config_registry()
    reset_provider_config_registry()
    register_agent_config("claude", _MngrClaudeLikeConfig)
    register_provider_config("docker", ProviderInstanceConfig)
    try:
        yield
    finally:
        reset_agent_config_registry()
        reset_provider_config_registry()


# Each case is (label, base_layers, override_raw). ``base_layers`` are raw TOML-shaped
# dicts merged into the initial config (via the production ``merge_with``) to form the
# base accumulator; ``override_raw`` is parsed into a padded sparse layer. Both are
# built at *test* time (inside the parametrized function) so the registry fixture has
# already registered ``claude`` / ``docker``.
_MNGR_CASES: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = [
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


@pytest.mark.usefixtures("_registered_mngr_config_classes")
@pytest.mark.parametrize(
    "base_layers,override_raw", [pytest.param(layers, o, id=label) for label, layers, o in _MNGR_CASES]
)
def test_mngr_merge_with_matches_frozen_reference(
    base_layers: list[dict[str, Any]], override_raw: dict[str, Any]
) -> None:
    """The production (overlay-backed) ``MngrConfig.merge_with`` reproduces the frozen
    field-by-field reference exactly, compared at the user-visible (final-default-
    applied) stage on both sides."""
    base = _base_from_layers(*base_layers)
    override = _parse_layer(override_raw)
    expected = _finalize_like_loader(_reference_mngr_config_merge(base, override))
    actual = _finalize_like_loader(base.merge_with(override))
    assert actual == expected


@pytest.mark.usefixtures("_registered_mngr_config_classes")
def test_mngr_container_entry_subclass_is_preserved() -> None:
    """A ``claude`` (subclass) ``agent_types`` entry round-trips as the subclass
    through ``MngrConfig.merge_with``, so subclass-only fields survive."""
    base = _base_from_layers({"agent_types": {"c": {"parent_type": "claude", "auto_dismiss_dialogs": True}}})
    override = _parse_layer({"agent_types": {"c": {"parent_type": "claude", "cli_args": "--y"}}})
    actual = base.merge_with(override)
    entry = actual.agent_types[AgentTypeName("c")]
    assert type(entry) is _MngrClaudeLikeConfig
    assert entry.auto_dismiss_dialogs is True


def test_mngr_reference_does_not_call_mngr_merge_with() -> None:
    """Guard against the equivalence test becoming tautological: the frozen reference
    must not call ``MngrConfig.merge_with`` (it does invoke container *entries'* own
    ``merge_with``, which is legitimate -- that is the per-key sub-merge the old body
    performed). Inspecting the source for the absence of a ``MngrConfig`` self-merge
    is sufficient: the reference dispatches the top-level merge by hand, never through
    the production method under test.
    """
    source = inspect.getsource(_reference_mngr_config_merge)
    # The reference must construct the result directly (``base.__class__(...)``) and
    # must never delegate the whole-config merge to the method under test.
    assert "base.merge_with(override)" not in source
    assert "base.__class__(" in source


# =============================================================================
# _apply_custom_overrides_to_parent_config (parent_type inheritance) equivalence guard
# =============================================================================
#
# The ``parent_type`` inheritance path
# (``agent_config_registry._apply_custom_overrides_to_parent_config``) now computes
# its result via the same overlay node algebra (``merge_models_via_overlay`` with
# ``drop_field_names=_METADATA_FIELDS`` and ``serialize_as_any=True``), the
# class-switching variant that re-parses into ``type(parent)``. The section below
# freezes the *old* field-by-field body verbatim as
# ``_reference_apply_custom_overrides`` and asserts the production function reproduces
# it over the corpus from the (now-deleted) prototype test.
# ``_reference_apply_custom_overrides`` never calls
# ``_apply_custom_overrides_to_parent_config``, so the equality is a real check, not a
# tautology (guarded by ``test_parent_type_reference_does_not_call_production``).


def _reference_apply_custom_overrides(
    parent_config: AgentTypeConfig,
    custom_config: AgentTypeConfig,
) -> AgentTypeConfig:
    """The OLD ``_apply_custom_overrides_to_parent_config`` body, frozen verbatim as the
    "old" side of the equivalence guard.

    This is the exact field-by-field logic the production function had before being
    rewired onto the overlay algebra. It must stay independent of the new path (it does
    not call ``_apply_custom_overrides_to_parent_config``) so the property test below is
    a genuine old == new check. ``merge`` is the unified combine; the production code
    discards its narrowings (the ``[0]``), so this reference does too.
    """
    explicitly_set_fields = custom_config.model_fields_set
    if not explicitly_set_fields - _METADATA_FIELDS:
        return parent_config

    custom_values = custom_config.model_dump()
    parent_values = parent_config.model_dump()
    updates: list[tuple[str, Any]] = []
    for field_name in explicitly_set_fields:
        if field_name in _METADATA_FIELDS:
            continue
        field_info = custom_config.__class__.model_fields.get(field_name)
        if field_info is not None and is_settings_patch_field(field_info.metadata):
            updates.append((field_name, merge(parent_values.get(field_name) or {}, custom_values[field_name])[0]))
        else:
            updates.append((field_name, custom_values[field_name]))
    if not updates:
        return parent_config
    return parent_config.model_copy_update(*updates)


# Each case is ``(label, parent, custom)``. ``parent`` plays the role
# ``resolve_agent_type`` gives it: either a bare ``config_class()`` or a config already
# folded with a parent user block. ``custom`` is the child ``[agent_types.X]`` block,
# always carrying ``parent_type`` (and often ``plugin``) so the ``_METADATA_FIELDS``
# skip is exercised. The corpus spans: child sets disjoint / overlapping / no fields;
# ``settings_overrides`` bare / ``__extend`` / nested / ``__assign`` / accumulating
# across the parent+child boundary; subclass-only fields on child and/or parent;
# ``_METADATA_FIELDS`` present on the child (must be ignored); and a
# ``ClaudeAgentConfig`` parent so the class-switching crux is exercised.
_PARENT_TYPE_CASES: list[tuple[str, AgentTypeConfig, AgentTypeConfig]] = [
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


@pytest.mark.parametrize("parent,custom", [pytest.param(p, c, id=label) for label, p, c in _PARENT_TYPE_CASES])
def test_apply_custom_overrides_matches_frozen_reference(
    parent: AgentTypeConfig,
    custom: AgentTypeConfig,
) -> None:
    """The production (overlay-backed) ``_apply_custom_overrides_to_parent_config``
    reproduces the frozen field-by-field reference exactly, value-for-value, and the
    output is the *parent's* concrete class (class-switching)."""
    expected = _reference_apply_custom_overrides(parent, custom)
    actual = _apply_custom_overrides_to_parent_config(parent, custom)
    assert actual == expected
    assert type(actual) is type(expected)
    assert type(actual) is type(parent)


def test_apply_custom_overrides_two_call_resolve_pattern() -> None:
    """Reproduce ``resolve_agent_type``'s two-call sequence end to end: fold a parent
    user block onto bare defaults, then fold the child onto that. Each call goes through
    the production (overlay-backed) function and must match the frozen two-call result.
    """
    parent_user_config = _claude(auto_dismiss_dialogs=True, settings_overrides={"model": "sonnet"})
    custom_config = _claude(
        parent_type="claude",
        plugin="claude",
        cli_args=("--child",),
        settings_overrides={"permissions__extend": {"allow__extend": ["Bash(npm *)"]}},
    )

    # Frozen reference two-call path.
    expected_parent_base = _reference_apply_custom_overrides(ClaudeAgentConfig(), parent_user_config)
    expected = _reference_apply_custom_overrides(expected_parent_base, custom_config)

    # Production two-call path.
    actual_parent_base = _apply_custom_overrides_to_parent_config(ClaudeAgentConfig(), parent_user_config)
    actual = _apply_custom_overrides_to_parent_config(actual_parent_base, custom_config)

    assert actual_parent_base == expected_parent_base
    assert actual == expected
    assert type(actual) is ClaudeAgentConfig


def test_apply_custom_overrides_class_switching_base_child_into_claude_parent() -> None:
    """A base ``AgentTypeConfig`` child folded onto a ``ClaudeAgentConfig`` parent must
    yield a ``ClaudeAgentConfig`` -- the subclass-only fields supplied by the parent (the
    child never set them). This is the class-switching crux in isolation."""
    parent = ClaudeAgentConfig(auto_dismiss_dialogs=True, settings_overrides={"model": "sonnet"})
    custom = _agent(parent_type="claude", cli_args=("b",))
    expected = _reference_apply_custom_overrides(parent, custom)
    actual = _apply_custom_overrides_to_parent_config(parent, custom)
    assert actual == expected
    assert type(actual) is ClaudeAgentConfig
    # The parent's subclass-only field survives (the child could not have set it).
    assert actual.auto_dismiss_dialogs is True


def test_parent_type_reference_does_not_call_production() -> None:
    """Guard against the equivalence test becoming tautological: the frozen reference
    must not call the production ``_apply_custom_overrides_to_parent_config`` it
    reproduces. The reference performs the field-by-field merge by hand, never through
    the function under test."""
    source = inspect.getsource(_reference_apply_custom_overrides)
    assert "_apply_custom_overrides_to_parent_config(" not in source
    assert "parent_config.model_copy_update(" in source


# =============================================================================
# Overlay (with Static* re-marking) vs detect_settings_narrowing equivalence
# =============================================================================
#
# Migrating ALL narrowing onto the overlay path: the overlay pipeline strips ``Static*``
# markers in ``model_dump`` and so would wrongly flag a replacement of an atomic aggregate
# (a string-shaped ``cli_args``, a provider's ``allowed_ssh_cidrs``, an explicit
# ``StaticList`` / ``StaticDict``) as narrowing. ``_overlay_all_narrowing_paths`` now
# re-marks those values after the dump. This section proves that, with re-marking, the
# overlay path reproduces the model-walking ``detect_settings_narrowing`` *exactly* for the
# non-settings fields (the settings-patch subset is filtered out, matching what the walker
# itself exempts). A mismatch on any corpus case is a real semantic divergence, not a test
# bug, and must be reported rather than worked around.


class _ProviderWithScalarTuple(ProviderInstanceConfig):
    """Stand-in provider subclass carrying a ``ScalarStrTuple`` field (mirroring the AWS
    provider's ``allowed_ssh_cidrs``), so the corpus exercises the ``ScalarTuple`` /
    narrowing-exempt re-marking inside a container entry without depending on the aws
    package. The ``ScalarStrTuple`` after-validator runs under ``model_validate`` (which
    ``_parse_providers`` uses), wrapping the value in ``ScalarTuple``.
    """

    allowed_ssh_cidrs: ScalarStrTuple = Field(default=("0.0.0.0/0",))


@pytest.fixture
def _registered_narrowing_classes() -> Iterator[None]:
    """Register the stand-in container-entry classes for the narrowing-equivalence corpus:
    ``claude`` -> the settings-bearing agent subclass, and ``docker`` -> the provider
    subclass carrying ``allowed_ssh_cidrs`` (so provider blocks with that field parse and
    its ``ScalarTuple`` re-marking is exercised). Reset both registries after each test."""
    reset_agent_config_registry()
    reset_provider_config_registry()
    register_agent_config("claude", _MngrClaudeLikeConfig)
    register_provider_config("docker", _ProviderWithScalarTuple)
    try:
        yield
    finally:
        reset_agent_config_registry()
        reset_provider_config_registry()


# Each case is ``(label, base_layers, override_raw)`` -- the same shape as ``_MNGR_CASES``:
# ``base_layers`` are raw TOML-shaped dicts merged into the initial config to form the
# base accumulator, ``override_raw`` is parsed into a padded sparse layer. The corpus
# deliberately spans every narrowing shape the spec calls out.
_NARROWING_CASES: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = [
    # --- scalar override: nothing narrows ---
    ("scalar_override", [{"prefix": "base-"}], {"prefix": "ovr-"}),
    ("empty_override", [{"prefix": "base-"}], {}),
    # --- list/tuple field: narrowed (drop an entry) ---
    ("list_field_narrowed", [{"unset_vars": ["A", "B", "C"]}], {"unset_vars": ["A", "B"]}),
    # --- list/tuple field: cleared ([] over non-empty) ---
    ("list_field_cleared", [{"unset_vars": ["A", "B"]}], {"unset_vars": []}),
    # --- list/tuple field: superset (no narrowing) ---
    ("list_field_superset", [{"unset_vars": ["A", "B"]}], {"unset_vars": ["A", "B", "C"]}),
    # --- list/tuple field: unchanged (no narrowing) ---
    ("list_field_unchanged", [{"unset_vars": ["A", "B"]}], {"unset_vars": ["A", "B"]}),
    # --- enabled_backends list narrowed / replaced ---
    ("backends_replaced", [{"enabled_backends": ["docker", "modal"]}], {"enabled_backends": ["docker"]}),
    # --- cli_args as a STRING (-> StringDerivedTuple, EXEMPT) over a list ---
    (
        "cli_args_string_over_list_exempt",
        [{"agent_types": {"a": {"cli_args": ["x", "y", "z"]}}}],
        {"agent_types": {"a": {"cli_args": "--only one"}}},
    ),
    # --- cli_args as a LIST that drops entries (-> should narrow) ---
    (
        "cli_args_list_drops_narrows",
        [{"agent_types": {"a": {"cli_args": ["x", "y", "z"]}}}],
        {"agent_types": {"a": {"cli_args": ["x", "y"]}}},
    ),
    # --- provider allowed_ssh_cidrs (ScalarStrTuple, EXEMPT) replaced with a narrower tuple ---
    (
        "allowed_ssh_cidrs_replaced_exempt",
        [{"providers": {"p": {"backend": "docker", "allowed_ssh_cidrs": ["0.0.0.0/0", "10.0.0.0/8"]}}}],
        {"providers": {"p": {"backend": "docker", "allowed_ssh_cidrs": ["203.0.113.4/32"]}}},
    ),
    # --- container entry ADDED: no narrowing ---
    (
        "agent_types_entry_added",
        [{"agent_types": {"foo": {"command": "c", "env": ["A=1", "B=2"]}}}],
        {"agent_types": {"bar": {"command": "d"}}},
    ),
    (
        "providers_entry_added",
        [{"providers": {"p": {"backend": "docker"}}}],
        {"providers": {"q": {"backend": "docker"}}},
    ),
    (
        "plugins_entry_added",
        [{"plugins": {"pl": {"enabled": True}}}],
        {"plugins": {"other": {"enabled": False}}},
    ),
    # --- container entry whose INNER aggregate narrows ---
    (
        "agent_types_inner_env_narrows",
        [{"agent_types": {"a": {"env": ["A=1", "B=2", "C=3"]}}}],
        {"agent_types": {"a": {"env": ["A=1"]}}},
    ),
    (
        "agent_types_inner_env_superset",
        [{"agent_types": {"a": {"env": ["A=1", "B=2"]}}}],
        {"agent_types": {"a": {"env": ["A=1", "B=2", "C=3"]}}},
    ),
    # --- dict-valued aggregate fields (commands' nested defaults / pre_command_scripts) ---
    (
        "pre_command_scripts_dict_narrows",
        [{"pre_command_scripts": {"create": ["echo a"], "start": ["echo b"]}}],
        {"pre_command_scripts": {"create": ["echo a"]}},
    ),
    (
        "work_dir_extra_paths_narrows",
        [{"work_dir_extra_paths": {"/a": "COPY", "/b": "SHARE"}}],
        {"work_dir_extra_paths": {"/a": "COPY"}},
    ),
    # --- nested model with a dropped inner list inside a command's defaults dict ---
    (
        "commands_defaults_inner_list_narrows",
        [{"commands": {"create": {"new_host": "docker", "extra": ["a", "b"]}}}],
        {"commands": {"create": {"extra": ["a"]}}},
    ),
    # --- settings_overrides narrowing (settings-patch -> must be FILTERED OUT on both sides) ---
    (
        "settings_overrides_present",
        [{"agent_types": {"c": {"parent_type": "claude", "settings_overrides": {"model": "sonnet", "k": 1}}}}],
        {"agent_types": {"c": {"parent_type": "claude", "settings_overrides": {"model": "opus"}}}},
    ),
    # --- mixture: scalar + narrowing list + exempt cli_args string + added entry ---
    (
        "combined_mixture",
        [
            {
                "prefix": "base-",
                "unset_vars": ["A", "B", "C"],
                "agent_types": {
                    "a": {"cli_args": ["x", "y", "z"], "env": ["E=1", "E=2"]},
                    "keep": {"command": "kept"},
                },
            }
        ],
        {
            "prefix": "ovr-",
            "unset_vars": ["A"],
            "agent_types": {
                "a": {"cli_args": "--single", "env": ["E=1"]},
                "new": {"command": "added"},
            },
        },
    ),
]


def _overlay_non_settings_narrowings(base: MngrConfig, override: MngrConfig) -> list[str]:
    """Run ``_overlay_all_narrowing_paths`` with the SAME kwargs ``MngrConfig.merge_with_narrowings``
    uses, then drop the ``SettingsPatchField``-rooted paths (reusing the production
    ``_is_settings_patch_narrowing``) -- the subset ``detect_settings_narrowing`` exempts.
    """
    settings_patch_field_names = get_settings_patch_field_names(type(override))
    base_fields = dict(base)
    override_fields = dict(override)
    merged_classes: dict[str, dict[Any, type[Any]]] = {
        field_name: {
            **{key: type(value) for key, value in (base_fields.get(field_name) or {}).items()},
            **{key: type(value) for key, value in (override_fields.get(field_name) or {}).items()},
        }
        for field_name in _CONTAINER_DICT_FIELDS
    }
    all_paths = _overlay_all_narrowing_paths(
        base,
        override,
        settings_patch_field_names=settings_patch_field_names,
        serialize_as_any=True,
        container_dict_field_names=_CONTAINER_DICT_FIELDS,
        drop_none_values=True,
        settings_patch_field_names_for_class=get_settings_patch_field_names,
    )
    return [
        dotted_path
        for dotted_path in all_paths
        if not _is_settings_patch_narrowing(
            dotted_path,
            settings_patch_field_names,
            _CONTAINER_DICT_FIELDS,
            merged_classes,
            get_settings_patch_field_names,
        )
    ]


@pytest.mark.usefixtures("_registered_narrowing_classes")
@pytest.mark.parametrize(
    "base_layers,override_raw", [pytest.param(layers, o, id=label) for label, layers, o in _NARROWING_CASES]
)
def test_overlay_non_settings_narrowing_matches_walker(
    base_layers: list[dict[str, Any]], override_raw: dict[str, Any]
) -> None:
    """With ``Static*`` re-marking, the NON-settings subset of the overlay narrowing paths
    reproduces ``detect_settings_narrowing`` exactly over the corpus. Compared as sorted
    lists so order is irrelevant. A failure here is a real semantic divergence between the
    overlay narrowing detector and the model-walker -- do not weaken this assertion."""
    base = _base_from_layers(*base_layers)
    override = _parse_layer(override_raw)
    overlay_paths = sorted(_overlay_non_settings_narrowings(base, override))
    walker_paths = sorted(detect_settings_narrowing(base, override))
    assert overlay_paths == walker_paths
