"""Tests for config data types."""

from pathlib import Path
from typing import Annotated
from typing import Any

import pytest
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import CommandDefaults
from imbue.mngr.config.data_types import CreateTemplate
from imbue.mngr.config.data_types import CreateTemplateName
from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.config.data_types import HookDefinition
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.config.data_types import RetryConfig
from imbue.mngr.config.data_types import TmuxConfig
from imbue.mngr.config.data_types import WorkDirExtraPathMode
from imbue.mngr.config.data_types import get_or_create_user_id
from imbue.mngr.config.data_types import split_cli_args_string
from imbue.mngr.config.field_markers import SettingsPatchField
from imbue.mngr.config.loader import parse_config
from imbue.mngr.config.overlay_merge import merge_models_via_overlay
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.errors import ParseSpecError
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import LifecycleHook
from imbue.mngr.primitives import LogLevel
from imbue.mngr.primitives import PluginName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.logging import LoggingConfig
from imbue.overlay.markers import ScalarTuple
from imbue.overlay.markers import StaticList
from imbue.overlay.markers import StaticTuple
from imbue.overlay.narrowing import would_assignment_narrow


class _TestAgentTypeConfig(AgentTypeConfig):
    """Test subclass with an extra field for testing subclass-specific field handling."""

    custom_flag: bool = Field(default=False)


class _PatchFieldAgentConfig(AgentTypeConfig):
    """Test subclass with a ``SettingsPatchField``-marked dict (accumulates across
    layers) and a plain dict (assign-by-default), to exercise marker-aware merge."""

    settings_overrides: Annotated[dict[str, Any], SettingsPatchField()] = Field(default_factory=dict)
    plain_map: dict[str, Any] = Field(default_factory=dict)


def test_merge_with_combines_settings_patch_field_same_key_extend() -> None:
    """Two scopes both extending the same nested key accumulate (not clobber)."""
    base = _PatchFieldAgentConfig.model_construct(
        settings_overrides={"permissions__extend": {"allow__extend": ["A"]}},
    )
    override = _PatchFieldAgentConfig.model_construct(
        settings_overrides={"permissions__extend": {"allow__extend": ["B"]}},
    )
    merged, _ = merge_models_via_overlay(base, override)
    assert merged.settings_overrides == {"permissions__extend": {"allow__extend": ["A", "B"]}}


def test_merge_with_combines_settings_patch_field_disjoint_keys() -> None:
    """Two scopes setting DIFFERENT keys both survive (no whole-dict replace)."""
    base = _PatchFieldAgentConfig.model_construct(
        settings_overrides={"permissions__extend": {"allow__extend": ["A"]}},
    )
    override = _PatchFieldAgentConfig.model_construct(
        settings_overrides={"model": "opus"},
    )
    merged, _ = merge_models_via_overlay(base, override)
    assert merged.settings_overrides == {
        "permissions__extend": {"allow__extend": ["A"]},
        "model": "opus",
    }


def test_merge_with_assigns_unmarked_dict_field_by_default() -> None:
    """A non-marked dict field still assigns by default (whole-dict replace)."""
    base = _PatchFieldAgentConfig.model_construct(plain_map={"a": 1, "b": 2})
    override = _PatchFieldAgentConfig.model_construct(plain_map={"c": 3})
    merged, _ = merge_models_via_overlay(base, override)
    assert merged.plain_map == {"c": 3}


def test_env_var_from_string_parses_simple_pair() -> None:
    """EnvVar.from_string should parse KEY=value format."""
    env_var = EnvVar.from_string("KEY=value")
    assert env_var.key == "KEY"
    assert env_var.value == "value"


def test_env_var_from_string_handles_equals_in_value() -> None:
    """EnvVar.from_string should handle equals signs in value."""
    env_var = EnvVar.from_string("KEY=val=ue")
    assert env_var.key == "KEY"
    assert env_var.value == "val=ue"


def test_env_var_from_string_strips_whitespace() -> None:
    """EnvVar.from_string should strip whitespace from key and value."""
    env_var = EnvVar.from_string("  KEY  =  value  ")
    assert env_var.key == "KEY"
    assert env_var.value == "value"


def test_env_var_from_string_raises_on_missing_equals() -> None:
    """EnvVar.from_string should raise ValueError when no equals sign."""
    with pytest.raises(ValueError, match="must be in KEY=VALUE format"):
        EnvVar.from_string("INVALID")


def test_env_var_from_string_handles_empty_value() -> None:
    """EnvVar.from_string should handle empty value after equals."""
    env_var = EnvVar.from_string("KEY=")
    assert env_var.key == "KEY"
    assert env_var.value == ""


def test_hook_definition_from_string_parses_valid_hook() -> None:
    """HookDefinition.from_string should parse valid hook definition."""
    hook_def = HookDefinition.from_string("initialize:echo 'hello'")
    assert hook_def.hook == LifecycleHook.INITIALIZE
    assert hook_def.command == "echo 'hello'"


def test_hook_definition_from_string_normalizes_hyphens_to_underscores() -> None:
    """HookDefinition.from_string should normalize hyphens to underscores."""
    hook_def = HookDefinition.from_string("on-create:cmd")
    assert hook_def.hook == LifecycleHook.ON_CREATE


def test_hook_definition_from_string_handles_colons_in_command() -> None:
    """HookDefinition.from_string should handle colons in command."""
    hook_def = HookDefinition.from_string("initialize:echo a:b:c")
    assert hook_def.command == "echo a:b:c"


def test_hook_definition_from_string_raises_on_invalid_hook_name() -> None:
    """HookDefinition.from_string should raise ValueError for invalid hook."""
    with pytest.raises(ValueError, match="Invalid hook name"):
        HookDefinition.from_string("invalid-hook:cmd")


def test_hook_definition_from_string_raises_on_missing_colon() -> None:
    """HookDefinition.from_string should raise ValueError when no colon."""
    with pytest.raises(ValueError, match="must be in NAME:COMMAND format"):
        HookDefinition.from_string("invalid")


def test_agent_type_config_merge_with_overrides_parent_type() -> None:
    """merge_models_via_overlay should override parent type."""
    base = AgentTypeConfig(parent_type=AgentTypeName("claude"))
    override = AgentTypeConfig(parent_type=AgentTypeName("codex"))
    merged, _ = merge_models_via_overlay(base, override)
    assert merged.parent_type == AgentTypeName("codex")


def test_agent_type_config_merge_with_overrides_command() -> None:
    """merge_models_via_overlay should override command."""
    base = AgentTypeConfig(command=CommandString("cmd1"))
    override = AgentTypeConfig(command=CommandString("cmd2"))
    merged, _ = merge_models_via_overlay(base, override)
    assert merged.command == CommandString("cmd2")


def test_agent_type_config_merge_with_replaces_cli_args() -> None:
    """merge_models_via_overlay assigns cli_args from override (no concat)."""
    base = AgentTypeConfig(cli_args=("--arg1",))
    override = AgentTypeConfig(cli_args=("--arg2",))
    merged, _ = merge_models_via_overlay(base, override)
    assert merged.cli_args == ("--arg2",)


def test_agent_type_config_merge_with_handles_empty_base_cli_args() -> None:
    """merge_models_via_overlay should handle empty base cli_args."""
    base = AgentTypeConfig(cli_args=())
    override = AgentTypeConfig(cli_args=("--arg",))
    merged, _ = merge_models_via_overlay(base, override)
    assert merged.cli_args == ("--arg",)


def test_agent_type_config_merge_with_replaces_with_empty_override_cli_args() -> None:
    """merge_models_via_overlay assigns even an empty override (assign-by-default)."""
    base = AgentTypeConfig(cli_args=("--arg",))
    override = AgentTypeConfig(cli_args=())
    merged, _ = merge_models_via_overlay(base, override)
    assert merged.cli_args == ()


def test_agent_type_config_merge_with_replaces_extra_provision_command() -> None:
    """merge_models_via_overlay assigns extra_provision_command from override."""
    base = AgentTypeConfig(extra_provision_command=("echo base",))
    override = AgentTypeConfig(extra_provision_command=("echo override",))
    merged, _ = merge_models_via_overlay(base, override)
    assert merged.extra_provision_command == ("echo override",)


def test_agent_type_config_merge_with_replaces_env() -> None:
    """merge_models_via_overlay assigns env from override (no concat)."""
    base = AgentTypeConfig(env=("FOO=1",))
    override = AgentTypeConfig(env=("BAR=2",))
    merged, _ = merge_models_via_overlay(base, override)
    assert merged.env == ("BAR=2",)


def test_agent_type_config_merge_with_replaces_upload_file() -> None:
    """merge_models_via_overlay assigns upload_file from override (no concat)."""
    base = AgentTypeConfig(upload_file=("a.txt:/a.txt",))
    override = AgentTypeConfig(upload_file=("b.txt:/b.txt",))
    merged, _ = merge_models_via_overlay(base, override)
    assert merged.upload_file == ("b.txt:/b.txt",)


def test_agent_type_config_merge_with_preserves_unset_provisioning_fields() -> None:
    """Base provisioning fields are preserved when override doesn't touch them."""
    base = AgentTypeConfig(extra_provision_command=("echo setup",), env=("KEY=val",))
    override = AgentTypeConfig(cli_args=("--flag",))
    merged, _ = merge_models_via_overlay(base, override)
    assert merged.extra_provision_command == ("echo setup",)
    assert merged.env == ("KEY=val",)
    assert merged.cli_args == ("--flag",)


def test_agent_type_config_merge_with_preserves_subclass_fields() -> None:
    """Subclass-specific fields not in override are preserved."""
    base = _TestAgentTypeConfig.model_construct(
        custom_flag=True,
        cli_args=("--base",),
    )
    override = _TestAgentTypeConfig.model_construct(
        cli_args=("--override",),
    )
    merged, _ = merge_models_via_overlay(base, override)
    assert isinstance(merged, _TestAgentTypeConfig)
    assert merged.cli_args == ("--override",)
    assert merged.custom_flag is True


def test_agent_type_config_merge_with_overrides_subclass_fields_when_set() -> None:
    """merge_models_via_overlay should override subclass fields that were explicitly set."""
    base = _TestAgentTypeConfig(custom_flag=True)
    override = _TestAgentTypeConfig.model_construct(custom_flag=False)
    merged, _ = merge_models_via_overlay(base, override)
    assert isinstance(merged, _TestAgentTypeConfig)
    assert merged.custom_flag is False


def test_agent_type_config_merge_with_accepts_base_class_override() -> None:
    """merge_models_via_overlay on a subclass should accept a base-class override."""
    base = _TestAgentTypeConfig(custom_flag=True, cli_args=("--base",))
    override = AgentTypeConfig.model_construct(cli_args=("--override",))
    merged, _ = merge_models_via_overlay(base, override)
    assert isinstance(merged, _TestAgentTypeConfig)
    assert merged.cli_args == ("--override",)
    assert merged.custom_flag is True


# =============================================================================
# Tests for ProviderInstanceConfig
# =============================================================================


class _TestProviderConfigWithListAndDict(ProviderInstanceConfig):
    """Test config with list and dict fields for testing merge behavior."""

    tags: list[str] = Field(default_factory=list)
    options: dict[str, str] = Field(default_factory=dict)


# =============================================================================
# Tests for MngrConfig.merge_with
# =============================================================================


def test_mngr_config_merge_with_overrides_prefix(mngr_test_prefix: str) -> None:
    """MngrConfig.merge_with should override prefix."""
    base = MngrConfig(prefix=f"{mngr_test_prefix}base-")
    override = MngrConfig(prefix=f"{mngr_test_prefix}override-")
    merged, _ = base.merge_with(override)
    assert merged.prefix == f"{mngr_test_prefix}override-"


def test_mngr_config_merge_with_overrides_default_host_dir(mngr_test_prefix: str) -> None:
    """MngrConfig.merge_with should override default_host_dir."""
    base = MngrConfig(prefix=mngr_test_prefix, default_host_dir=Path("/base"))
    override = MngrConfig(prefix=mngr_test_prefix, default_host_dir=Path("/override"))
    merged, _ = base.merge_with(override)
    assert merged.default_host_dir == Path("/override")


def test_mngr_config_merge_with_replaces_unset_vars(mngr_test_prefix: str) -> None:
    """MngrConfig.merge_with assigns unset_vars from override (no concat)."""
    base = MngrConfig(prefix=mngr_test_prefix, unset_vars=["VAR1", "VAR2"])
    override = MngrConfig(prefix=mngr_test_prefix, unset_vars=["VAR3"])
    merged, _ = base.merge_with(override)
    assert merged.unset_vars == ["VAR3"]


def test_mngr_config_merge_with_none_base_retry_takes_override() -> None:
    """``merge_with`` must not call ``.merge_with`` on a ``None`` base sub-model. A base
    whose ``retry``/``logging`` is ``None`` (as a ``model_construct``'d layer can be)
    merged with an override that sets them takes the override outright, instead of
    raising ``AttributeError`` on ``None.merge_with``."""
    base = MngrConfig().model_copy_update(("retry", None), ("logging", None))
    assert base.retry is None and base.logging is None
    override = MngrConfig(retry=RetryConfig(connect_retry_times=7))
    merged, _ = base.merge_with(override)
    assert merged.retry == RetryConfig(connect_retry_times=7)


def test_mngr_config_merge_with_none_override_retry_keeps_base() -> None:
    """When the override's ``retry``/``logging`` is ``None``, the base value is kept."""
    base = MngrConfig(retry=RetryConfig(connect_retry_times=9))
    override = MngrConfig().model_copy_update(("retry", None), ("logging", None))
    merged, _ = base.merge_with(override)
    assert merged.retry == RetryConfig(connect_retry_times=9)


def test_mngr_config_merge_with_merges_agent_types_per_key(mngr_test_prefix: str) -> None:
    """agent_types is a container dict: same-key entries are merged per-key by the overlay pipeline."""
    base = MngrConfig(
        prefix=mngr_test_prefix, agent_types={AgentTypeName("claude"): AgentTypeConfig(cli_args=("--base",))}
    )
    override = MngrConfig(
        prefix=mngr_test_prefix, agent_types={AgentTypeName("claude"): AgentTypeConfig(cli_args=("--override",))}
    )
    merged, _ = base.merge_with(override)
    # cli_args is assign-by-default at the AgentTypeConfig level.
    assert merged.agent_types[AgentTypeName("claude")].cli_args == ("--override",)


def test_mngr_config_merge_with_adds_new_agent_types(mngr_test_prefix: str) -> None:
    """MngrConfig.merge_with should add new agent types from override."""
    base = MngrConfig(
        prefix=mngr_test_prefix, agent_types={AgentTypeName("claude"): AgentTypeConfig(cli_args=("--base",))}
    )
    override = MngrConfig(
        prefix=mngr_test_prefix, agent_types={AgentTypeName("codex"): AgentTypeConfig(cli_args=("--codex",))}
    )
    merged, _ = base.merge_with(override)
    assert AgentTypeName("claude") in merged.agent_types
    assert AgentTypeName("codex") in merged.agent_types


def test_mngr_config_merge_with_merges_providers(mngr_test_prefix: str) -> None:
    """MngrConfig.merge_with should merge providers dicts."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        providers={
            ProviderInstanceName("local"): ProviderInstanceConfig(backend=ProviderBackendName("local")),
        },
    )
    override = MngrConfig(
        prefix=mngr_test_prefix,
        providers={
            ProviderInstanceName("docker"): ProviderInstanceConfig(backend=ProviderBackendName("docker")),
        },
    )
    merged, _ = base.merge_with(override)
    assert ProviderInstanceName("local") in merged.providers
    assert ProviderInstanceName("docker") in merged.providers


def test_mngr_config_merge_with_merges_same_provider_key(mngr_test_prefix: str) -> None:
    """MngrConfig.merge_with should merge configs when both have the same provider key."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        providers={
            ProviderInstanceName("my-docker"): ProviderInstanceConfig(backend=ProviderBackendName("docker")),
        },
    )
    override = MngrConfig(
        prefix=mngr_test_prefix,
        providers={
            ProviderInstanceName("my-docker"): ProviderInstanceConfig(backend=ProviderBackendName("modal")),
        },
    )
    merged, _ = base.merge_with(override)
    assert ProviderInstanceName("my-docker") in merged.providers
    assert merged.providers[ProviderInstanceName("my-docker")].backend == ProviderBackendName("modal")


def test_mngr_config_merge_with_merges_plugins(mngr_test_prefix: str) -> None:
    """MngrConfig.merge_with should merge plugins dicts."""
    base = MngrConfig(prefix=mngr_test_prefix, plugins={PluginName("plugin1"): PluginConfig(enabled=True)})
    override = MngrConfig(prefix=mngr_test_prefix, plugins={PluginName("plugin1"): PluginConfig(enabled=False)})
    merged, _ = base.merge_with(override)
    assert merged.plugins[PluginName("plugin1")].enabled is False


def test_mngr_config_merge_with_adds_new_plugins(mngr_test_prefix: str) -> None:
    """MngrConfig.merge_with should add new plugins from override."""
    base = MngrConfig(prefix=mngr_test_prefix, plugins={PluginName("plugin1"): PluginConfig(enabled=True)})
    override = MngrConfig(prefix=mngr_test_prefix, plugins={PluginName("plugin2"): PluginConfig(enabled=True)})
    merged, _ = base.merge_with(override)
    assert PluginName("plugin1") in merged.plugins
    assert PluginName("plugin2") in merged.plugins


def test_mngr_config_merge_with_merges_commands(mngr_test_prefix: str) -> None:
    """MngrConfig.merge_with should merge commands dicts."""
    base = MngrConfig(prefix=mngr_test_prefix, commands={"create": CommandDefaults(defaults={"name": "base"})})
    override = MngrConfig(prefix=mngr_test_prefix, commands={"create": CommandDefaults(defaults={"name": "override"})})
    merged, _ = base.merge_with(override)
    assert merged.commands["create"].defaults["name"] == "override"


def test_mngr_config_merge_with_adds_new_commands(mngr_test_prefix: str) -> None:
    """MngrConfig.merge_with should add new commands from override."""
    base = MngrConfig(prefix=mngr_test_prefix, commands={"create": CommandDefaults(defaults={"name": "base"})})
    override = MngrConfig(prefix=mngr_test_prefix, commands={"list": CommandDefaults(defaults={"format": "json"})})
    merged, _ = base.merge_with(override)
    assert "create" in merged.commands
    assert "list" in merged.commands


def test_mngr_config_merge_with_merges_logging(mngr_test_prefix: str) -> None:
    """MngrConfig.merge_with should merge logging config."""
    base = MngrConfig(prefix=mngr_test_prefix, logging=LoggingConfig(file_level=LogLevel.DEBUG))
    override = MngrConfig(prefix=mngr_test_prefix, logging=LoggingConfig(file_level=LogLevel.TRACE))
    merged, _ = base.merge_with(override)
    assert merged.logging.file_level == LogLevel.TRACE


# =============================================================================
# Tests for MngrConfig.create_templates
# =============================================================================


def test_mngr_config_merge_with_merges_create_templates_per_key(mngr_test_prefix: str) -> None:
    """create_templates is a container dict: same-key entries are merged per-key by the overlay pipeline."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={
            CreateTemplateName("modal"): CreateTemplate(options={"new_host": "modal", "target_path": "/base"}),
        },
    )
    override = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={
            CreateTemplateName("modal"): CreateTemplate(options={"target_path": "/override"}),
        },
    )
    merged, _ = base.merge_with(override)
    modal_template = merged.create_templates[CreateTemplateName("modal")]
    # options is assign-by-default at the CreateTemplate level.
    assert modal_template.options == {"target_path": "/override"}


def test_mngr_config_merge_with_adds_new_create_templates(mngr_test_prefix: str) -> None:
    """MngrConfig.merge_with should add new create_templates from override."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={CreateTemplateName("modal"): CreateTemplate(options={"new_host": "modal"})},
    )
    override = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={CreateTemplateName("docker"): CreateTemplate(options={"new_host": "docker"})},
    )
    merged, _ = base.merge_with(override)
    assert CreateTemplateName("modal") in merged.create_templates
    assert CreateTemplateName("docker") in merged.create_templates


def test_mngr_config_create_templates_default_is_empty_dict(mngr_test_prefix: str) -> None:
    """MngrConfig should have empty create_templates by default."""
    config = MngrConfig(prefix=mngr_test_prefix)
    assert config.create_templates == {}


# =============================================================================
# Tests for MngrConfig.pre_command_scripts
# =============================================================================


def test_mngr_config_merge_with_replaces_pre_command_scripts(mngr_test_prefix: str) -> None:
    """pre_command_scripts is a leaf dict (not in the container carveout): assign-by-default."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        pre_command_scripts={"create": ["echo base"], "list": ["echo list"]},
    )
    override = MngrConfig(
        prefix=mngr_test_prefix,
        pre_command_scripts={"create": ["echo override"]},
    )
    merged, _ = base.merge_with(override)
    # Whole dict replaced; the unrelated "list" entry is dropped.
    assert merged.pre_command_scripts == {"create": ["echo override"]}


def test_mngr_config_merge_with_preserves_pre_command_scripts_when_override_does_not_touch_them(
    mngr_test_prefix: str,
) -> None:
    """When override doesn't set pre_command_scripts, the base value survives."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        pre_command_scripts={"create": ["echo create"]},
    )
    override = MngrConfig.model_construct(prefix=mngr_test_prefix, pre_command_scripts=None)
    merged, _ = base.merge_with(override)
    assert merged.pre_command_scripts == {"create": ["echo create"]}


def test_mngr_config_pre_command_scripts_default_is_empty_dict(mngr_test_prefix: str) -> None:
    """MngrConfig should have empty pre_command_scripts by default."""
    config = MngrConfig(prefix=mngr_test_prefix)
    assert config.pre_command_scripts == {}


# =============================================================================
# Tests for MngrConfig.work_dir_extra_paths
# =============================================================================


def test_mngr_config_work_dir_extra_paths_default_is_empty_dict(mngr_test_prefix: str) -> None:
    """MngrConfig should have empty work_dir_extra_paths by default."""
    config = MngrConfig(prefix=mngr_test_prefix)
    assert config.work_dir_extra_paths == {}


def test_mngr_config_merge_with_replaces_work_dir_extra_paths(mngr_test_prefix: str) -> None:
    """work_dir_extra_paths is a leaf dict: assign-by-default replaces the whole map."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        work_dir_extra_paths={".venv": WorkDirExtraPathMode.SHARE, ".test_output": WorkDirExtraPathMode.COPY},
    )
    override = MngrConfig(
        prefix=mngr_test_prefix,
        work_dir_extra_paths={".venv": WorkDirExtraPathMode.COPY},
    )
    merged, _ = base.merge_with(override)
    assert merged.work_dir_extra_paths == {".venv": WorkDirExtraPathMode.COPY}


def test_mngr_config_merge_with_preserves_work_dir_extra_paths_when_override_does_not_touch(
    mngr_test_prefix: str,
) -> None:
    """When override doesn't set work_dir_extra_paths, base survives."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        work_dir_extra_paths={".venv": WorkDirExtraPathMode.SHARE},
    )
    override = MngrConfig.model_construct(prefix=mngr_test_prefix, work_dir_extra_paths=None)
    merged, _ = base.merge_with(override)
    assert merged.work_dir_extra_paths == {".venv": WorkDirExtraPathMode.SHARE}


# =============================================================================
# Tests for ProviderInstanceConfig.is_enabled
# =============================================================================


def test_provider_instance_config_is_enabled_default_true() -> None:
    """ProviderInstanceConfig.is_enabled should default to True."""
    config = ProviderInstanceConfig(backend=ProviderBackendName("local"))
    assert config.is_enabled is None


def test_provider_instance_config_is_enabled_can_be_set_false() -> None:
    """ProviderInstanceConfig.is_enabled can be set to False."""
    config = ProviderInstanceConfig(backend=ProviderBackendName("local"), is_enabled=False)
    assert config.is_enabled is False


# =============================================================================
# Tests for MngrConfig.enabled_backends
# =============================================================================


def test_mngr_config_enabled_backends_default_empty(mngr_test_prefix: str) -> None:
    """MngrConfig.enabled_backends should default to empty list (all backends enabled)."""
    config = MngrConfig(prefix=mngr_test_prefix)
    assert config.enabled_backends == []


def test_mngr_config_enabled_backends_can_be_set(mngr_test_prefix: str) -> None:
    """MngrConfig.enabled_backends can be set to specific backends."""
    config = MngrConfig(
        prefix=mngr_test_prefix,
        enabled_backends=[ProviderBackendName("local"), ProviderBackendName("docker")],
    )
    assert ProviderBackendName("local") in config.enabled_backends
    assert ProviderBackendName("docker") in config.enabled_backends


def test_mngr_config_merge_enabled_backends_override_wins_when_not_empty(mngr_test_prefix: str) -> None:
    """MngrConfig merge assigns override's enabled_backends when it is set."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        enabled_backends=[ProviderBackendName("local"), ProviderBackendName("docker")],
    )
    override = MngrConfig(
        prefix=mngr_test_prefix,
        enabled_backends=[ProviderBackendName("modal")],
    )
    merged, _ = base.merge_with(override)
    assert merged.enabled_backends == [ProviderBackendName("modal")]


def test_mngr_config_merge_enabled_backends_replaces_with_empty(mngr_test_prefix: str) -> None:
    """An explicit empty enabled_backends in the override replaces base under assign-by-default."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        enabled_backends=[ProviderBackendName("local")],
    )
    override = MngrConfig(
        prefix=mngr_test_prefix,
        enabled_backends=[],
    )
    merged, _ = base.merge_with(override)
    # Empty list is a real assignment, not "unset"; base is replaced.
    assert merged.enabled_backends == []


def test_mngr_config_merge_enabled_backends_preserves_base_when_override_does_not_touch(
    mngr_test_prefix: str,
) -> None:
    """When override doesn't set enabled_backends (None), base wins."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        enabled_backends=[ProviderBackendName("local")],
    )
    override = MngrConfig.model_construct(prefix=mngr_test_prefix, enabled_backends=None)
    merged, _ = base.merge_with(override)
    assert merged.enabled_backends == [ProviderBackendName("local")]


# =============================================================================
# Tests for MngrConfig.is_remote_agent_installation_allowed
# =============================================================================


def test_mngr_config_merge_is_remote_agent_installation_allowed_override_wins(mngr_test_prefix: str) -> None:
    """MngrConfig merge should use override's is_remote_agent_installation_allowed when set."""
    base = MngrConfig(prefix=mngr_test_prefix, is_remote_agent_installation_allowed=True)
    override = MngrConfig(prefix=mngr_test_prefix, is_remote_agent_installation_allowed=False)
    merged, _ = base.merge_with(override)
    assert merged.is_remote_agent_installation_allowed is False


# =============================================================================
# Tests for split_cli_args_string
# =============================================================================


def test_split_cli_args_string_simple_args() -> None:
    """split_cli_args_string should split simple arguments on whitespace."""
    result = split_cli_args_string("--verbose --model gpt-4")
    assert result == ("--verbose", "--model", "gpt-4")


def test_split_cli_args_string_preserves_single_quotes() -> None:
    """split_cli_args_string should preserve single-quoted values."""
    result = split_cli_args_string('--settings \'{"key": "value"}\'')
    assert result == ("--settings", '\'{"key": "value"}\'')
    assert " ".join(result) == '--settings \'{"key": "value"}\''


def test_split_cli_args_string_preserves_double_quotes() -> None:
    """split_cli_args_string should preserve double-quoted values."""
    result = split_cli_args_string('--flag "value with spaces"')
    assert result == ("--flag", '"value with spaces"')
    assert " ".join(result) == '--flag "value with spaces"'


def test_split_cli_args_string_empty_string() -> None:
    """split_cli_args_string should return empty tuple for empty string."""
    result = split_cli_args_string("")
    assert result == ()


def test_split_cli_args_string_complex_json_with_single_quotes() -> None:
    """split_cli_args_string should preserve complex JSON wrapped in single quotes."""
    cli_args = (
        """--settings '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "./scripts/check.sh"}]}]}}'"""
    )
    result = split_cli_args_string(cli_args)
    assert len(result) == 2
    assert result[0] == "--settings"
    # The JSON value should still be wrapped in single quotes
    assert result[1].startswith("'")
    assert result[1].endswith("'")
    # Round-trip: joining should produce the original string
    assert " ".join(result) == cli_args


def test_split_cli_args_string_single_arg() -> None:
    """split_cli_args_string should handle a single argument."""
    result = split_cli_args_string("--verbose")
    assert result == ("--verbose",)


def test_split_cli_args_string_preserves_quoting_for_assemble_command() -> None:
    """Verify that cli_args parsed from a string produce correct commands when joined.

    This is the end-to-end scenario: TOML string -> split -> tuple -> join -> command.
    """
    cli_args_str = """--settings '{"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "read INPUT; SID=$(echo \\"$INPUT\\" | jq -r \\".session_id // empty\\"); [ -n \\"$SID\\" ] && [ -n \\"${CLAUDE_ENV_FILE:-}\\" ] && echo \\"export MNGR_CLAUDE_SESSION_ID=$SID\\" >> \\"$CLAUDE_ENV_FILE\\" || true"}]}]}}'"""
    parts = split_cli_args_string(cli_args_str)
    reassembled = " ".join(parts)
    assert reassembled == cli_args_str


def test_split_cli_args_string_does_not_treat_hash_as_comment() -> None:
    """split_cli_args_string should not treat '#' as a comment character."""
    hash_token = "#channel"
    cli_args = f"--flag {hash_token} --other"
    result = split_cli_args_string(cli_args)
    assert len(result) == 3
    assert result[0] == "--flag"
    assert result[1] == hash_token
    assert result[2] == "--other"


# =============================================================================
# Tests for destroyed_host_persisted_seconds
# =============================================================================


def test_provider_instance_config_destroyed_host_persisted_seconds_defaults_to_none() -> None:
    config = ProviderInstanceConfig(backend=ProviderBackendName("local"))
    assert config.destroyed_host_persisted_seconds is None


def test_provider_instance_config_destroyed_host_persisted_seconds_can_be_set() -> None:
    config = ProviderInstanceConfig(
        backend=ProviderBackendName("modal"),
        destroyed_host_persisted_seconds=86400.0,
    )
    assert config.destroyed_host_persisted_seconds == 86400.0


def test_mngr_config_default_destroyed_host_persisted_seconds_is_seven_days(mngr_test_prefix: str) -> None:
    config = MngrConfig(prefix=mngr_test_prefix)
    assert config.default_destroyed_host_persisted_seconds == 60.0 * 60.0 * 24.0 * 7.0


def test_mngr_config_merge_overrides_default_destroyed_host_persisted_seconds(mngr_test_prefix: str) -> None:
    base = MngrConfig(prefix=mngr_test_prefix, default_destroyed_host_persisted_seconds=604800.0)
    override = MngrConfig(prefix=mngr_test_prefix, default_destroyed_host_persisted_seconds=86400.0)
    merged, _ = base.merge_with(override)
    assert merged.default_destroyed_host_persisted_seconds == 86400.0


def test_mngr_config_merge_keeps_base_destroyed_host_persisted_seconds_when_override_none(
    mngr_test_prefix: str,
) -> None:
    base = MngrConfig(prefix=mngr_test_prefix, default_destroyed_host_persisted_seconds=86400.0)
    override = MngrConfig.model_construct(
        prefix=mngr_test_prefix,
        default_destroyed_host_persisted_seconds=None,
    )
    merged, _ = base.merge_with(override)
    assert merged.default_destroyed_host_persisted_seconds == 86400.0


# =============================================================================
# Tests for min_online_host_age_seconds
# =============================================================================


def test_provider_instance_config_min_online_host_age_seconds_defaults_to_none() -> None:
    config = ProviderInstanceConfig(backend=ProviderBackendName("test"))
    assert config.min_online_host_age_seconds is None


def test_mngr_config_default_min_online_host_age_seconds_is_ten_minutes(mngr_test_prefix: str) -> None:
    config = MngrConfig(prefix=mngr_test_prefix)
    assert config.default_min_online_host_age_seconds == 60.0 * 10.0


def test_mngr_config_merge_overrides_default_min_online_host_age_seconds(mngr_test_prefix: str) -> None:
    base = MngrConfig(prefix=mngr_test_prefix, default_min_online_host_age_seconds=600.0)
    override = MngrConfig(prefix=mngr_test_prefix, default_min_online_host_age_seconds=300.0)
    merged, _ = base.merge_with(override)
    assert merged.default_min_online_host_age_seconds == 300.0


def test_mngr_config_merge_keeps_base_min_online_host_age_seconds_when_override_none(
    mngr_test_prefix: str,
) -> None:
    base = MngrConfig(prefix=mngr_test_prefix, default_min_online_host_age_seconds=300.0)
    override = MngrConfig.model_construct(prefix=mngr_test_prefix, default_min_online_host_age_seconds=None)
    merged, _ = base.merge_with(override)
    assert merged.default_min_online_host_age_seconds == 300.0


def test_mngr_config_merge_overrides_connect_command(mngr_test_prefix: str) -> None:
    base = MngrConfig(prefix=mngr_test_prefix, connect_command="base-cmd")
    override = MngrConfig(prefix=mngr_test_prefix, connect_command="override-cmd")
    merged, _ = base.merge_with(override)
    assert merged.connect_command == "override-cmd"


def test_mngr_config_merge_keeps_base_connect_command_when_override_none(mngr_test_prefix: str) -> None:
    base = MngrConfig(prefix=mngr_test_prefix, connect_command="base-cmd")
    override = MngrConfig.model_construct(prefix=mngr_test_prefix, connect_command=None)
    merged, _ = base.merge_with(override)
    assert merged.connect_command == "base-cmd"


def test_mngr_config_connect_command_defaults_to_none(mngr_test_prefix: str) -> None:
    config = MngrConfig(prefix=mngr_test_prefix)
    assert config.connect_command is None


# =============================================================================
# Tests for MngrConfig.merge_with completeness
# =============================================================================


# =============================================================================
# Tests for cross-scope narrowing surfaced by the overlay merge (``merge_with``)
# =============================================================================


def test_settings_narrowing_flags_list_replacement(mngr_test_prefix: str) -> None:
    """Replacing a non-empty list with a different non-empty list is flagged."""
    base = MngrConfig(prefix=mngr_test_prefix, unset_vars=["BASE"])
    override = MngrConfig(prefix=mngr_test_prefix, unset_vars=["OTHER"])
    _, narrowings = base.merge_with(override)
    assert narrowings == ["unset_vars"]


def test_settings_narrowing_allows_superset_list(mngr_test_prefix: str) -> None:
    """A list override that contains every base entry (e.g. from __extend) is not narrowing."""
    base = MngrConfig(prefix=mngr_test_prefix, unset_vars=["BASE"])
    override = MngrConfig(prefix=mngr_test_prefix, unset_vars=["BASE", "EXTRA"])
    _, narrowings = base.merge_with(override)
    assert narrowings == []


class _NestedContainerNamed(FrozenModel):
    """A sub-model with a field named like a top-level container (``commands``)."""

    commands: dict[str, str] = Field(default_factory=dict)


class _OuterWithNested(FrozenModel):
    nested: _NestedContainerNamed = Field(default_factory=_NestedContainerNamed)


def test_settings_narrowing_flags_drop_in_nested_container_named_field() -> None:
    """A sub-model field named like a top-level container dict (``commands``) is
    narrowing-checked as a leaf aggregate -- not mis-treated as a top-level container
    (which per-key recurses and silently skips dropped keys). The container match is
    guarded by a top-level depth check, so only an actual top-level field qualifies; a
    nested namesake (non-empty path) falls through to the leaf check."""
    base = _OuterWithNested(nested=_NestedContainerNamed(commands={"a": "x"}))
    override = _OuterWithNested(nested=_NestedContainerNamed(commands={"b": "y"}))
    # ``_OuterWithNested`` is a plain model (no ``merge_with``); drive the overlay merge directly.
    _, narrowings = merge_models_via_overlay(base, override)
    assert narrowings == ["nested.commands"]


def test_settings_narrowing_flags_empty_override_clearing_non_empty_base(mngr_test_prefix: str) -> None:
    """Clearing a non-empty value with an explicit empty override is the most
    extreme narrowing case (every base entry is dropped) and must be flagged
    unless the user opts in via ``allow_settings_key_assignment_narrowing``.

    The earlier behavior exempted empty overrides as "deliberate clears", but
    that loophole defeats the safety net for users whose base values come from
    defaults (a freshly-applied empty override would silently wipe them).
    """
    base = MngrConfig(prefix=mngr_test_prefix, unset_vars=["BASE"])
    override = MngrConfig(prefix=mngr_test_prefix, unset_vars=[])
    _, narrowings = base.merge_with(override)
    assert narrowings == ["unset_vars"]


def test_settings_narrowing_ignores_empty_override_over_empty_base(mngr_test_prefix: str) -> None:
    """An empty override over an empty base is a no-op and not flagged."""
    base = MngrConfig(prefix=mngr_test_prefix, unset_vars=[])
    override = MngrConfig(prefix=mngr_test_prefix, unset_vars=[])
    _, narrowings = base.merge_with(override)
    assert narrowings == []


def test_settings_narrowing_ignores_unwritten_layer_field(mngr_test_prefix: str) -> None:
    """A layer that doesn't write a field (``parse_config`` defaults it to None)
    never narrows the base, even when the base is non-empty.

    Regression test for the "defaults silently clear earlier layers" concern.
    """
    base = MngrConfig(prefix=mngr_test_prefix, unset_vars=["BASE_VAR"])
    # A layer that touches only an unrelated field -- parse_config leaves
    # unset_vars at None so the merge can fall back to base.
    override = parse_config({"prefix": "other-"}, disabled_plugins=frozenset())
    _, narrowings = base.merge_with(override)
    assert narrowings == []


def test_settings_narrowing_recurses_into_command_defaults(mngr_test_prefix: str) -> None:
    """Per-key recursion through ``commands`` (a container dict) and ``CommandDefaults.defaults``
    flags the deepest path where data is actually lost.
    """
    base = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"env": ["X=4"], "branch": "main"})},
    )
    override = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"env": ["X=4"], "branch": "main", "extra": "x"})},
    )
    # Override is a superset -- no narrowing.
    _, narrowings = base.merge_with(override)
    assert narrowings == []

    override_drops_branch = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"env": ["X=4"]})},
    )
    # Override drops the "branch" key from defaults -- flagged at the defaults level.
    _, narrowings = base.merge_with(override_drops_branch)
    assert narrowings == ["commands.create.defaults"]


def test_settings_narrowing_flags_nested_value_replacement(mngr_test_prefix: str) -> None:
    """When a shared dict key's value is itself a non-empty aggregate being replaced,
    the deeper path is flagged.
    """
    base = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"env": ["X=4"]})},
    )
    override = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"env": ["X=5"]})},
    )
    _, narrowings = base.merge_with(override)
    assert narrowings == ["commands.create.defaults.env"]


# === Narrowing detection across agent_types / providers / create_templates / plugins ===
#
# These container dicts all use per-key additive merge at the top level, so adding
# a new entry never narrows. Within each entry, the sub-model fields use assign-
# by-default, so narrowing applies the same way it does for MngrConfig direct
# attributes. The tests below mirror the user's stated requirement that all three
# (four, with plugins) mechanisms honour the same __extend / narrowing semantics.


def test_settings_narrowing_allows_adding_new_agent_type_entry(mngr_test_prefix: str) -> None:
    """Adding a new agent_type key in a higher layer never narrows -- the
    container-level merge is per-key additive, so the base entry survives."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        agent_types={AgentTypeName("a"): AgentTypeConfig(cli_args=("--x",))},
    )
    override = MngrConfig(
        prefix=mngr_test_prefix,
        agent_types={AgentTypeName("b"): AgentTypeConfig(cli_args=("--y",))},
    )
    _, narrowings = base.merge_with(override)
    assert narrowings == []


def test_settings_narrowing_flags_agent_type_cli_args_replacement(mngr_test_prefix: str) -> None:
    """Reassigning ``agent_types.<name>.cli_args`` over a non-empty base is narrowing."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        agent_types={AgentTypeName("my_claude"): AgentTypeConfig(cli_args=("--debug",))},
    )
    override = MngrConfig(
        prefix=mngr_test_prefix,
        agent_types={AgentTypeName("my_claude"): AgentTypeConfig(cli_args=("--verbose",))},
    )
    _, narrowings = base.merge_with(override)
    assert narrowings == ["agent_types.my_claude.cli_args"]


def test_settings_narrowing_flags_agent_type_cli_args_clearing(mngr_test_prefix: str) -> None:
    """Explicitly clearing ``agent_types.<name>.cli_args`` to an empty tuple still narrows."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        agent_types={AgentTypeName("my_claude"): AgentTypeConfig(cli_args=("--debug",))},
    )
    override = MngrConfig(
        prefix=mngr_test_prefix,
        agent_types={AgentTypeName("my_claude"): AgentTypeConfig(cli_args=())},
    )
    _, narrowings = base.merge_with(override)
    assert narrowings == ["agent_types.my_claude.cli_args"]


def test_settings_narrowing_allows_agent_type_cli_args_superset(mngr_test_prefix: str) -> None:
    """An assign that includes every base entry (e.g. the materialised result of
    ``cli_args__extend``) preserves all prior entries and does not narrow."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        agent_types={AgentTypeName("my_claude"): AgentTypeConfig(cli_args=("--debug",))},
    )
    override = MngrConfig(
        prefix=mngr_test_prefix,
        agent_types={AgentTypeName("my_claude"): AgentTypeConfig(cli_args=("--debug", "--verbose"))},
    )
    _, narrowings = base.merge_with(override)
    assert narrowings == []


def test_settings_narrowing_still_flags_plain_tuple_override(mngr_test_prefix: str) -> None:
    """Sanity check that the ``ScalarTuple`` marker is the actual discriminator: the
    same shape with a plain ``tuple`` override is still flagged as narrowing.
    Together with ``test_settings_narrowing_exempts_scalar_tuple_override``, this
    proves the marker is what gates the exemption rather than some incidental shape
    property.
    """
    base = MngrConfig(
        prefix=mngr_test_prefix,
        agent_types={AgentTypeName("my_claude"): AgentTypeConfig(cli_args=("--debug",))},
    )
    override = MngrConfig.model_construct(
        prefix=mngr_test_prefix,
        agent_types={AgentTypeName("my_claude"): AgentTypeConfig.model_construct(cli_args=("--verbose",))},
    )
    _, narrowings = base.merge_with(override)
    assert narrowings == ["agent_types.my_claude.cli_args"]


def test_would_assignment_narrow_exempts_scalar_tuple() -> None:
    """``would_assignment_narrow`` mirrors the leaf-level marker exemption: a
    ``ScalarTuple`` override (e.g. a string-derived ``cli_args``) over a non-empty
    list/tuple base reports no narrowing, while a plain-tuple override with the same
    tokens still does. This is the rule the template-application guard in
    ``apply_create_template`` relies on.
    """
    base: tuple[str, ...] = ("--debug",)
    assert would_assignment_narrow(base, ScalarTuple(("--verbose",))) is False
    assert would_assignment_narrow(base, ("--verbose",)) is True


def test_scalar_tuple_is_a_static_tuple() -> None:
    """``ScalarTuple`` is a ``StaticTuple``, so the generalized ``Static*`` exemption
    covers a string-derived ``cli_args`` value -- its narrowing-exemption behavior is
    unchanged."""
    assert isinstance(ScalarTuple(("--verbose",)), StaticTuple)


def test_settings_narrowing_exempts_static_list_override(mngr_test_prefix: str) -> None:
    """The ``StaticList`` exemption holds through the full ``merge_with`` narrowing
    detection; a plain-list override of the same shape still narrows."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        agent_types={AgentTypeName("my_claude"): AgentTypeConfig(cli_args=("--debug", "--trace"))},
    )
    exempt_override = MngrConfig.model_construct(
        prefix=mngr_test_prefix,
        agent_types={AgentTypeName("my_claude"): AgentTypeConfig.model_construct(cli_args=StaticList(["--verbose"]))},
    )
    _, narrowings = base.merge_with(exempt_override)
    assert narrowings == []
    plain_override = MngrConfig.model_construct(
        prefix=mngr_test_prefix,
        agent_types={AgentTypeName("my_claude"): AgentTypeConfig.model_construct(cli_args=("--verbose",))},
    )
    _, narrowings = base.merge_with(plain_override)
    assert narrowings == ["agent_types.my_claude.cli_args"]


def test_settings_narrowing_exempts_scalar_tuple_override(mngr_test_prefix: str) -> None:
    """The leaf-level ``ScalarTuple`` exemption holds through the full ``merge_with``
    narrowing detection: a ``ScalarTuple`` override over a non-empty tuple base reports
    no narrowing, while a plain-tuple override of the same shape still does.
    """
    base = MngrConfig(
        prefix=mngr_test_prefix,
        agent_types={AgentTypeName("my_claude"): AgentTypeConfig(cli_args=("--debug",))},
    )
    exempt_override = MngrConfig.model_construct(
        prefix=mngr_test_prefix,
        agent_types={
            AgentTypeName("my_claude"): AgentTypeConfig.model_construct(cli_args=ScalarTuple(("--verbose",)))
        },
    )
    _, narrowings = base.merge_with(exempt_override)
    assert narrowings == []
    plain_override = MngrConfig.model_construct(
        prefix=mngr_test_prefix,
        agent_types={AgentTypeName("my_claude"): AgentTypeConfig.model_construct(cli_args=("--verbose",))},
    )
    _, narrowings = base.merge_with(plain_override)
    assert narrowings == ["agent_types.my_claude.cli_args"]


def test_settings_narrowing_flags_provider_subclass_list_replacement(mngr_test_prefix: str) -> None:
    """A provider sub-config's list field follows the same narrowing rule via
    sub-model recursion. Uses ``_TestProviderConfigWithListAndDict`` because
    ``ProviderInstanceConfig`` itself has no list fields (those are added by
    backend-specific subclasses)."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        providers={
            ProviderInstanceName("my_p"): _TestProviderConfigWithListAndDict(
                backend=ProviderBackendName("local"),
                tags=["base"],
                options={},
            )
        },
    )
    override = MngrConfig(
        prefix=mngr_test_prefix,
        providers={
            ProviderInstanceName("my_p"): _TestProviderConfigWithListAndDict(
                backend=ProviderBackendName("local"),
                tags=["other"],
                options={},
            )
        },
    )
    _, narrowings = base.merge_with(override)
    assert narrowings == ["providers.my_p.tags"]


def test_settings_narrowing_flags_provider_subclass_dict_replacement(mngr_test_prefix: str) -> None:
    """A provider sub-config's dict field also follows the narrowing rule."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        providers={
            ProviderInstanceName("my_p"): _TestProviderConfigWithListAndDict(
                backend=ProviderBackendName("local"),
                tags=[],
                options={"k1": "v1", "k2": "v_base"},
            )
        },
    )
    # Override drops "k1" entirely -- narrowing.
    override = MngrConfig(
        prefix=mngr_test_prefix,
        providers={
            ProviderInstanceName("my_p"): _TestProviderConfigWithListAndDict(
                backend=ProviderBackendName("local"),
                tags=[],
                options={"k2": "v_override"},
            )
        },
    )
    _, narrowings = base.merge_with(override)
    assert narrowings == ["providers.my_p.options"]


def test_settings_narrowing_flags_create_template_options_replacement(mngr_test_prefix: str) -> None:
    """Re-assigning a list value inside ``create_templates.<name>.options`` is flagged
    at the deepest path (``options.<param>``) where the loss actually happens."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={CreateTemplateName("dev"): CreateTemplate(options={"env": ["X=1"]})},
    )
    override = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={CreateTemplateName("dev"): CreateTemplate(options={"env": ["X=2"]})},
    )
    _, narrowings = base.merge_with(override)
    assert narrowings == ["create_templates.dev.options.env"]


def test_settings_narrowing_flags_create_template_options_key_drop(mngr_test_prefix: str) -> None:
    """Re-assigning ``create_templates.<name>.options`` to a dict missing a base key
    flags at the ``options`` level (the dict itself was truncated)."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={CreateTemplateName("dev"): CreateTemplate(options={"env": ["X=1"], "name": "agent"})},
    )
    # Override drops "name" -- the whole options dict has been narrowed.
    override = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={CreateTemplateName("dev"): CreateTemplate(options={"env": ["X=1"]})},
    )
    _, narrowings = base.merge_with(override)
    assert narrowings == ["create_templates.dev.options"]


def test_settings_narrowing_allows_create_template_options_superset(mngr_test_prefix: str) -> None:
    """An override that preserves every base options key (and value) does not narrow."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={CreateTemplateName("dev"): CreateTemplate(options={"env": ["X=1"]})},
    )
    override = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={CreateTemplateName("dev"): CreateTemplate(options={"env": ["X=1"], "name": "agent"})},
    )
    _, narrowings = base.merge_with(override)
    assert narrowings == []


class _TestPluginConfigWithListField(PluginConfig):
    """Plugin sub-config with a list field, used by the plugin narrowing test."""

    items: list[str] = Field(default_factory=list)


def test_settings_narrowing_flags_plugin_subclass_list_replacement(mngr_test_prefix: str) -> None:
    """Plugin sub-configs (subclasses of PluginConfig with extra fields) follow the
    same narrowing rule. Plugin configs are routinely extended by plugin authors with
    list / dict fields; the safety net must reach them too."""
    base = MngrConfig(
        prefix=mngr_test_prefix,
        plugins={PluginName("my-plugin"): _TestPluginConfigWithListField(enabled=True, items=["a"])},
    )
    override = MngrConfig(
        prefix=mngr_test_prefix,
        plugins={PluginName("my-plugin"): _TestPluginConfigWithListField(enabled=True, items=["b"])},
    )
    _, narrowings = base.merge_with(override)
    assert narrowings == ["plugins.my-plugin.items"]


def _build_fully_populated_mngr_config(mngr_test_prefix: str) -> MngrConfig:
    """Construct a MngrConfig with every field set to a non-default value.

    Helper for the merge_with round-trip test below.
    """
    return MngrConfig(
        prefix=f"{mngr_test_prefix}override-",
        default_host_dir=Path("/tmp/non-default-host-dir"),
        unset_vars=["NON_DEFAULT_VAR"],
        work_dir_extra_paths={".something": WorkDirExtraPathMode.COPY},
        pager="bat",
        enabled_backends=[ProviderBackendName("local")],
        agent_types={AgentTypeName("custom"): AgentTypeConfig(cli_args=("--non-default",))},
        providers={ProviderInstanceName("custom"): ProviderInstanceConfig(backend=ProviderBackendName("docker"))},
        plugins={PluginName("custom-plugin"): PluginConfig(enabled=False)},
        disabled_plugins=frozenset({"some-plugin"}),
        commands={"create": CommandDefaults(defaults={"connect": False})},
        create_templates={CreateTemplateName("my-template"): CreateTemplate(options={"new_host": "modal"})},
        pre_command_scripts={"create": ["echo non-default"]},
        retry=RetryConfig(connect_retry_times=999, connect_retry_delay="123s"),
        logging=LoggingConfig(file_level=LogLevel.TRACE),
        tmux=TmuxConfig(
            primary_window_name="non-default-window",
            attach_args=("-CC",),
            additional_config_path=Path("/tmp/non-default-tmux.conf"),
        ),
        is_remote_agent_installation_allowed=False,
        connect_command="non-default-connect",
        is_nested_tmux_allowed=True,
        headless=True,
        is_error_reporting_enabled=False,
        is_allowed_in_pytest=True,
        default_destroyed_host_persisted_seconds=98765.0,
        default_min_online_host_age_seconds=4321.0,
        agent_ready_timeout=42.0,
        allow_settings_key_assignment_narrowing=True,
    )


def test_mngr_config_merge_with_round_trips_every_field(mngr_test_prefix: str) -> None:
    """Round-trip test: every MngrConfig field survives merge_with(empty_override).

    Ensures that ``MngrConfig.merge_with`` does not silently drop any field.
    When a new field is added to MngrConfig but not threaded through
    ``merge_with``, the merged result will diverge from the populated base on
    that field and the assertion below will fail with a clear "extra/missing
    items" diff.

    Step 1: build a fully-populated MngrConfig with every field set to a
        non-default value, then assert that fact (so a future refactor that
        accidentally lands a value matching the default also surfaces here).
    Step 2: merge with an empty override (``MngrConfig.model_construct()`` --
        no fields set), and verify the result equals the populated base.
    """
    populated = _build_fully_populated_mngr_config(mngr_test_prefix)

    # Step 1: confirm every field on `populated` differs from MngrConfig's
    # default. ``MngrConfig.model_construct()`` materializes default values
    # without running validators, so we compare against that reference.
    defaults = MngrConfig()
    populated_dump = populated.model_dump()
    defaults_dump = defaults.model_dump()
    fields_matching_default = {name for name in MngrConfig.model_fields if populated_dump[name] == defaults_dump[name]}
    assert not fields_matching_default, (
        "Round-trip test setup must give every field a non-default value, but the "
        f"following fields match MngrConfig defaults: {sorted(fields_matching_default)}. "
        "Update _build_fully_populated_mngr_config to set them to non-default values."
    )

    # Step 2: merging with an empty override must preserve every field.
    # ``parse_config({})`` faithfully reproduces what parse_config emits for
    # an empty TOML file -- scalar fields become None (the "unset" marker
    # ``MngrConfig.merge_with`` keys off), container dicts become ``{}``,
    # etc. Using ``MngrConfig.model_construct()`` here would *not* work
    # because pydantic fills in defaults for fields not passed, making the
    # override look like every default-valued field was explicitly set.
    empty_override = parse_config({}, disabled_plugins=frozenset())
    merged, _ = populated.merge_with(empty_override)
    assert merged == populated


# =============================================================================
# Tests for TmuxConfig
# =============================================================================


def test_tmux_config_defaults() -> None:
    """TmuxConfig defaults preserve today's behavior: window named 'agent', no attach flags."""
    config = TmuxConfig()
    assert config.primary_window_name == "agent"
    assert config.attach_args == ()
    assert config.additional_config_path is None


def test_tmux_config_attach_args_accepts_string() -> None:
    """A string attach_args is shlex-split into tokens (mirrors cli_args)."""
    config = TmuxConfig.model_validate({"attach_args": "-CC -u"})
    assert config.attach_args == ("-CC", "-u")


def test_tmux_config_attach_args_accepts_list() -> None:
    config = TmuxConfig.model_validate({"attach_args": ["-CC"]})
    assert config.attach_args == ("-CC",)


def test_tmux_config_merge_only_overrides_set_fields() -> None:
    """An override that only sets attach_args must not clobber the base window name."""
    base = TmuxConfig(primary_window_name="custom", attach_args=("-2",))
    override = TmuxConfig.model_construct(attach_args=("-CC",))
    merged = base.merge_with(override)
    assert merged.primary_window_name == "custom"
    assert merged.attach_args == ("-CC",)


def test_tmux_config_merge_empty_override_is_noop() -> None:
    base = TmuxConfig(primary_window_name="custom", attach_args=("-CC",))
    merged = base.merge_with(TmuxConfig.model_construct())
    assert merged == base


def test_mngr_config_agent_session_name_is_prefix_plus_name() -> None:
    """agent_session_name is the single definition of the prefix + name session-name rule."""
    assert MngrConfig(prefix="mngr-").agent_session_name("my-agent") == "mngr-my-agent"
    assert MngrConfig(prefix="custom-").agent_session_name("foo") == "custom-foo"


# =============================================================================
# Tests for CreateTemplateName validation
# =============================================================================


def test_create_template_name_raises_on_empty_string() -> None:
    """CreateTemplateName should raise ParseSpecError for empty string."""
    with pytest.raises(ParseSpecError, match="Template name cannot be empty"):
        CreateTemplateName("")


# =============================================================================
# Tests for get_or_create_user_id with MNGR_USER_ID
# =============================================================================


def test_get_or_create_user_id_uses_env_var_when_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_or_create_user_id should use MNGR_USER_ID env var when file doesn't exist."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    env_user_id = "a" * 32
    monkeypatch.setenv("MNGR_USER_ID", env_user_id)

    result = get_or_create_user_id(profile_dir)
    assert result == env_user_id

    # Should have persisted the value
    user_id_file = profile_dir / "user_id"
    assert user_id_file.read_text() == env_user_id


def test_get_or_create_user_id_validates_env_var_matches_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_or_create_user_id should assert when MNGR_USER_ID doesn't match existing file."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    existing_id = "b" * 32
    user_id_file = profile_dir / "user_id"
    user_id_file.write_text(existing_id)

    monkeypatch.setenv("MNGR_USER_ID", "c" * 32)

    with pytest.raises(AssertionError, match="MNGR_USER_ID environment variable does not match"):
        get_or_create_user_id(profile_dir)


def test_get_or_create_user_id_accepts_env_var_matching_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_or_create_user_id should succeed when MNGR_USER_ID matches the existing file."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    existing_id = "d" * 32
    user_id_file = profile_dir / "user_id"
    user_id_file.write_text(existing_id)

    monkeypatch.setenv("MNGR_USER_ID", existing_id)

    result = get_or_create_user_id(profile_dir)
    assert result == existing_id


def test_mngr_context_get_profile_user_id(temp_mngr_ctx: MngrContext) -> None:
    """MngrContext.get_profile_user_id should return a non-empty user ID."""
    user_id = temp_mngr_ctx.get_profile_user_id()
    assert len(user_id) == 32


# =============================================================================
# MngrContext.get_plugin_config tests
# =============================================================================


def test_get_plugin_config_returns_default_when_absent(temp_mngr_ctx: MngrContext) -> None:
    """get_plugin_config should return a default instance when the plugin is not configured."""
    result = temp_mngr_ctx.get_plugin_config("nonexistent-plugin", PluginConfig)
    assert isinstance(result, PluginConfig)
    assert result.enabled is True


def test_get_plugin_config_raises_on_wrong_type(temp_mngr_ctx: MngrContext) -> None:
    """get_plugin_config should raise ConfigParseError when plugin config has wrong type."""

    # Register a PluginConfig for a plugin, then try to retrieve it as a different subclass
    class CustomPluginConfig(PluginConfig):
        custom_field: str = "default"

    updated_config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().plugins, {PluginName("typed-plugin"): PluginConfig(enabled=True)}),
    )
    ctx = temp_mngr_ctx.model_copy_update(
        to_update(temp_mngr_ctx.field_ref().config, updated_config),
    )

    # Requesting as CustomPluginConfig when it's stored as PluginConfig should raise
    with pytest.raises(ConfigParseError, match="expected CustomPluginConfig"):
        ctx.get_plugin_config("typed-plugin", CustomPluginConfig)


def test_get_plugin_config_returns_configured_value(temp_mngr_ctx: MngrContext) -> None:
    """get_plugin_config should return the configured PluginConfig when present."""
    plugin_config = PluginConfig(enabled=False)
    updated_config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().plugins, {PluginName("test-plugin"): plugin_config}),
    )
    ctx = temp_mngr_ctx.model_copy_update(
        to_update(temp_mngr_ctx.field_ref().config, updated_config),
    )

    result = ctx.get_plugin_config("test-plugin", PluginConfig)
    assert isinstance(result, PluginConfig)
    assert result.enabled is False
