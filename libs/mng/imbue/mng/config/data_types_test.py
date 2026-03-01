"""Tests for config data types."""

from pathlib import Path

import pytest
from pydantic import Field

from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import CommandDefaults
from imbue.mng.config.data_types import CreateTemplate
from imbue.mng.config.data_types import CreateTemplateName
from imbue.mng.config.data_types import EnvVar
from imbue.mng.config.data_types import HookDefinition
from imbue.mng.config.data_types import MngConfig
from imbue.mng.config.data_types import PluginConfig
from imbue.mng.config.data_types import ProviderInstanceConfig
from imbue.mng.config.data_types import merge_cli_args
from imbue.mng.config.data_types import merge_dict_fields
from imbue.mng.config.data_types import merge_list_fields
from imbue.mng.config.data_types import split_cli_args_string
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import LifecycleHook
from imbue.mng.primitives import LogLevel
from imbue.mng.primitives import Permission
from imbue.mng.primitives import PluginName
from imbue.mng.primitives import ProviderBackendName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.utils.logging import LoggingConfig


def test_logging_config_merge_overrides_all_fields() -> None:
    """Merging LoggingConfig should override all fields from override."""
    base = LoggingConfig()
    override = LoggingConfig(
        file_level=LogLevel.TRACE,
        log_dir=Path("/custom/logs"),
        max_log_files=500,
        max_log_size_mb=20,
        console_level=LogLevel.DEBUG,
        is_logging_commands=False,
        is_logging_command_output=True,
    )

    merged = base.merge_with(override)

    assert merged.file_level == LogLevel.TRACE
    assert merged.log_dir == Path("/custom/logs")
    assert merged.max_log_files == 500
    assert merged.max_log_size_mb == 20
    assert merged.console_level == LogLevel.DEBUG
    assert merged.is_logging_commands is False
    assert merged.is_logging_command_output is True


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
    """AgentTypeConfig.merge_with should override parent type."""
    base = AgentTypeConfig(parent_type=AgentTypeName("claude"))
    override = AgentTypeConfig(parent_type=AgentTypeName("codex"))
    merged = base.merge_with(override)
    assert merged.parent_type == AgentTypeName("codex")


def test_agent_type_config_merge_with_overrides_command() -> None:
    """AgentTypeConfig.merge_with should override command."""
    base = AgentTypeConfig(command=CommandString("cmd1"))
    override = AgentTypeConfig(command=CommandString("cmd2"))
    merged = base.merge_with(override)
    assert merged.command == CommandString("cmd2")


def test_agent_type_config_merge_with_concatenates_cli_args() -> None:
    """AgentTypeConfig.merge_with should concatenate cli_args."""
    base = AgentTypeConfig(cli_args=("--arg1",))
    override = AgentTypeConfig(cli_args=("--arg2",))
    merged = base.merge_with(override)
    assert merged.cli_args == ("--arg1", "--arg2")


def test_agent_type_config_merge_with_handles_empty_base_cli_args() -> None:
    """AgentTypeConfig.merge_with should handle empty base cli_args."""
    base = AgentTypeConfig(cli_args=())
    override = AgentTypeConfig(cli_args=("--arg",))
    merged = base.merge_with(override)
    assert merged.cli_args == ("--arg",)


def test_agent_type_config_merge_with_handles_empty_override_cli_args() -> None:
    """AgentTypeConfig.merge_with should keep base when override is empty."""
    base = AgentTypeConfig(cli_args=("--arg",))
    override = AgentTypeConfig(cli_args=())
    merged = base.merge_with(override)
    assert merged.cli_args == ("--arg",)


def test_agent_type_config_merge_with_concatenates_permissions() -> None:
    """AgentTypeConfig.merge_with should concatenate permissions."""
    base = AgentTypeConfig(permissions=[Permission("read")])
    override = AgentTypeConfig(permissions=[Permission("write")])
    merged = base.merge_with(override)
    assert merged.permissions == [Permission("read"), Permission("write")]


def test_merge_cli_args_concatenates_both_when_present() -> None:
    """merge_cli_args should concatenate when both present."""
    result = merge_cli_args(("--arg1",), ("--arg2",))
    assert result == ("--arg1", "--arg2")


def test_merge_cli_args_returns_override_when_base_empty() -> None:
    """merge_cli_args should return override when base is empty."""
    result = merge_cli_args((), ("--arg",))
    assert result == ("--arg",)


def test_merge_cli_args_returns_base_when_override_empty() -> None:
    """merge_cli_args should return base when override is empty."""
    result = merge_cli_args(("--arg",), ())
    assert result == ("--arg",)


def test_merge_cli_args_returns_empty_when_both_empty() -> None:
    """merge_cli_args should return empty when both empty."""
    result = merge_cli_args((), ())
    assert result == ()


def test_merge_list_fields_concatenates_when_override_not_none() -> None:
    """merge_list_fields should concatenate when override is not None."""
    result = merge_list_fields([1, 2], [3, 4])
    assert result == [1, 2, 3, 4]


def test_merge_list_fields_returns_base_when_override_none() -> None:
    """merge_list_fields should return base when override is None."""
    result = merge_list_fields([1, 2], None)
    assert result == [1, 2]


def test_merge_list_fields_concatenates_empty_override() -> None:
    """merge_list_fields should handle empty override list."""
    result = merge_list_fields([1, 2], [])
    assert result == [1, 2]


# =============================================================================
# Tests for merge_dict_fields
# =============================================================================


def test_merge_dict_fields_combines_keys() -> None:
    """merge_dict_fields should combine keys from both dicts."""
    result = merge_dict_fields({"a": 1, "b": 2}, {"c": 3})
    assert result == {"a": 1, "b": 2, "c": 3}


def test_merge_dict_fields_override_takes_precedence() -> None:
    """merge_dict_fields should use override value for same key."""
    result = merge_dict_fields({"a": 1, "b": 2}, {"b": 99})
    assert result == {"a": 1, "b": 99}


def test_merge_dict_fields_returns_base_when_override_none() -> None:
    """merge_dict_fields should return base when override is None."""
    result = merge_dict_fields({"a": 1}, None)
    assert result == {"a": 1}


def test_merge_dict_fields_returns_override_when_base_empty() -> None:
    """merge_dict_fields should return override when base is empty."""
    result = merge_dict_fields({}, {"a": 1})
    assert result == {"a": 1}


def test_merge_dict_fields_handles_empty_override() -> None:
    """merge_dict_fields should return base when override is empty dict."""
    result = merge_dict_fields({"a": 1}, {})
    assert result == {"a": 1}


# =============================================================================
# Tests for ProviderInstanceConfig
# =============================================================================


def test_provider_instance_config_merge_with_returns_override_backend() -> None:
    """ProviderInstanceConfig.merge_with should return override's backend."""
    base = ProviderInstanceConfig(backend=ProviderBackendName("local"))
    override = ProviderInstanceConfig(backend=ProviderBackendName("docker"))
    merged = base.merge_with(override)
    assert merged.backend == ProviderBackendName("docker")


class _TestProviderConfigWithListAndDict(ProviderInstanceConfig):
    """Test config with list and dict fields for testing merge behavior."""

    tags: list[str] = Field(default_factory=list)
    options: dict[str, str] = Field(default_factory=dict)


def test_provider_instance_config_merge_concatenates_lists() -> None:
    """ProviderInstanceConfig.merge_with should concatenate list fields."""
    base = _TestProviderConfigWithListAndDict(
        backend=ProviderBackendName("local"),
        tags=["tag1", "tag2"],
        options={},
    )
    override = _TestProviderConfigWithListAndDict(
        backend=ProviderBackendName("local"),
        tags=["tag3"],
        options={},
    )
    merged = base.merge_with(override)
    assert isinstance(merged, _TestProviderConfigWithListAndDict)
    assert merged.tags == ["tag1", "tag2", "tag3"]


def test_provider_instance_config_merge_merges_dicts() -> None:
    """ProviderInstanceConfig.merge_with should merge dict fields."""
    base = _TestProviderConfigWithListAndDict(
        backend=ProviderBackendName("local"),
        tags=[],
        options={"key1": "val1", "key2": "base_val"},
    )
    override = _TestProviderConfigWithListAndDict(
        backend=ProviderBackendName("local"),
        tags=[],
        options={"key2": "override_val", "key3": "val3"},
    )
    merged = base.merge_with(override)
    assert isinstance(merged, _TestProviderConfigWithListAndDict)
    assert merged.options == {"key1": "val1", "key2": "override_val", "key3": "val3"}


def test_provider_instance_config_merge_handles_none_list_override() -> None:
    """ProviderInstanceConfig.merge_with should keep base list when override is None."""
    base = _TestProviderConfigWithListAndDict(
        backend=ProviderBackendName("local"),
        tags=["tag1"],
        options={},
    )
    override = _TestProviderConfigWithListAndDict.model_construct(
        backend=ProviderBackendName("local"),
        tags=None,
        options={},
    )
    merged = base.merge_with(override)
    assert isinstance(merged, _TestProviderConfigWithListAndDict)
    assert merged.tags == ["tag1"]


def test_provider_instance_config_merge_handles_none_dict_override() -> None:
    """ProviderInstanceConfig.merge_with should keep base dict when override is None."""
    base = _TestProviderConfigWithListAndDict(
        backend=ProviderBackendName("local"),
        tags=[],
        options={"key1": "val1"},
    )
    override = _TestProviderConfigWithListAndDict.model_construct(
        backend=ProviderBackendName("local"),
        tags=[],
        options=None,
    )
    merged = base.merge_with(override)
    assert isinstance(merged, _TestProviderConfigWithListAndDict)
    assert merged.options == {"key1": "val1"}


# =============================================================================
# Tests for PluginConfig
# =============================================================================


def test_plugin_config_merge_with_overrides_enabled() -> None:
    """PluginConfig.merge_with should override enabled field."""
    base = PluginConfig(enabled=True)
    override = PluginConfig(enabled=False)
    merged = base.merge_with(override)
    assert merged.enabled is False


def test_plugin_config_merge_with_keeps_base_when_override_none() -> None:
    """PluginConfig.merge_with should keep base when override is None-ish."""
    base = PluginConfig(enabled=True)
    # model_construct bypasses validation, allowing us to test None behavior
    override = PluginConfig.model_construct(enabled=None)
    merged = base.merge_with(override)
    assert merged.enabled is True


# =============================================================================
# Tests for MngConfig.merge_with
# =============================================================================


def test_mng_config_merge_with_overrides_prefix(mng_test_prefix: str) -> None:
    """MngConfig.merge_with should override prefix."""
    base = MngConfig(prefix=f"{mng_test_prefix}base-")
    override = MngConfig(prefix=f"{mng_test_prefix}override-")
    merged = base.merge_with(override)
    assert merged.prefix == f"{mng_test_prefix}override-"


def test_mng_config_merge_with_overrides_default_host_dir(mng_test_prefix: str) -> None:
    """MngConfig.merge_with should override default_host_dir."""
    base = MngConfig(prefix=mng_test_prefix, default_host_dir=Path("/base"))
    override = MngConfig(prefix=mng_test_prefix, default_host_dir=Path("/override"))
    merged = base.merge_with(override)
    assert merged.default_host_dir == Path("/override")


def test_mng_config_merge_with_concatenates_unset_vars(mng_test_prefix: str) -> None:
    """MngConfig.merge_with should concatenate unset_vars."""
    base = MngConfig(prefix=mng_test_prefix, unset_vars=["VAR1", "VAR2"])
    override = MngConfig(prefix=mng_test_prefix, unset_vars=["VAR3"])
    merged = base.merge_with(override)
    assert "VAR1" in merged.unset_vars
    assert "VAR2" in merged.unset_vars
    assert "VAR3" in merged.unset_vars


def test_mng_config_merge_with_merges_agent_types(mng_test_prefix: str) -> None:
    """MngConfig.merge_with should merge agent_types dicts."""
    base = MngConfig(
        prefix=mng_test_prefix, agent_types={AgentTypeName("claude"): AgentTypeConfig(cli_args=("--base",))}
    )
    override = MngConfig(
        prefix=mng_test_prefix, agent_types={AgentTypeName("claude"): AgentTypeConfig(cli_args=("--override",))}
    )
    merged = base.merge_with(override)
    # cli_args should be concatenated
    assert merged.agent_types[AgentTypeName("claude")].cli_args == ("--base", "--override")


def test_mng_config_merge_with_adds_new_agent_types(mng_test_prefix: str) -> None:
    """MngConfig.merge_with should add new agent types from override."""
    base = MngConfig(
        prefix=mng_test_prefix, agent_types={AgentTypeName("claude"): AgentTypeConfig(cli_args=("--base",))}
    )
    override = MngConfig(
        prefix=mng_test_prefix, agent_types={AgentTypeName("codex"): AgentTypeConfig(cli_args=("--codex",))}
    )
    merged = base.merge_with(override)
    assert AgentTypeName("claude") in merged.agent_types
    assert AgentTypeName("codex") in merged.agent_types


def test_mng_config_merge_with_merges_providers(mng_test_prefix: str) -> None:
    """MngConfig.merge_with should merge providers dicts."""
    base = MngConfig(
        prefix=mng_test_prefix,
        providers={
            ProviderInstanceName("local"): ProviderInstanceConfig(backend=ProviderBackendName("local")),
        },
    )
    override = MngConfig(
        prefix=mng_test_prefix,
        providers={
            ProviderInstanceName("docker"): ProviderInstanceConfig(backend=ProviderBackendName("docker")),
        },
    )
    merged = base.merge_with(override)
    assert ProviderInstanceName("local") in merged.providers
    assert ProviderInstanceName("docker") in merged.providers


def test_mng_config_merge_with_merges_same_provider_key(mng_test_prefix: str) -> None:
    """MngConfig.merge_with should merge configs when both have the same provider key."""
    base = MngConfig(
        prefix=mng_test_prefix,
        providers={
            ProviderInstanceName("my-docker"): ProviderInstanceConfig(backend=ProviderBackendName("docker")),
        },
    )
    override = MngConfig(
        prefix=mng_test_prefix,
        providers={
            ProviderInstanceName("my-docker"): ProviderInstanceConfig(backend=ProviderBackendName("modal")),
        },
    )
    merged = base.merge_with(override)
    assert ProviderInstanceName("my-docker") in merged.providers
    assert merged.providers[ProviderInstanceName("my-docker")].backend == ProviderBackendName("modal")


def test_mng_config_merge_with_merges_plugins(mng_test_prefix: str) -> None:
    """MngConfig.merge_with should merge plugins dicts."""
    base = MngConfig(prefix=mng_test_prefix, plugins={PluginName("plugin1"): PluginConfig(enabled=True)})
    override = MngConfig(prefix=mng_test_prefix, plugins={PluginName("plugin1"): PluginConfig(enabled=False)})
    merged = base.merge_with(override)
    assert merged.plugins[PluginName("plugin1")].enabled is False


def test_mng_config_merge_with_adds_new_plugins(mng_test_prefix: str) -> None:
    """MngConfig.merge_with should add new plugins from override."""
    base = MngConfig(prefix=mng_test_prefix, plugins={PluginName("plugin1"): PluginConfig(enabled=True)})
    override = MngConfig(prefix=mng_test_prefix, plugins={PluginName("plugin2"): PluginConfig(enabled=True)})
    merged = base.merge_with(override)
    assert PluginName("plugin1") in merged.plugins
    assert PluginName("plugin2") in merged.plugins


def test_mng_config_merge_with_merges_commands(mng_test_prefix: str) -> None:
    """MngConfig.merge_with should merge commands dicts."""
    base = MngConfig(prefix=mng_test_prefix, commands={"create": CommandDefaults(defaults={"name": "base"})})
    override = MngConfig(prefix=mng_test_prefix, commands={"create": CommandDefaults(defaults={"name": "override"})})
    merged = base.merge_with(override)
    assert merged.commands["create"].defaults["name"] == "override"


def test_mng_config_merge_with_adds_new_commands(mng_test_prefix: str) -> None:
    """MngConfig.merge_with should add new commands from override."""
    base = MngConfig(prefix=mng_test_prefix, commands={"create": CommandDefaults(defaults={"name": "base"})})
    override = MngConfig(prefix=mng_test_prefix, commands={"list": CommandDefaults(defaults={"format": "json"})})
    merged = base.merge_with(override)
    assert "create" in merged.commands
    assert "list" in merged.commands


def test_mng_config_merge_with_merges_logging(mng_test_prefix: str) -> None:
    """MngConfig.merge_with should merge logging config."""
    base = MngConfig(prefix=mng_test_prefix, logging=LoggingConfig(file_level=LogLevel.DEBUG))
    override = MngConfig(prefix=mng_test_prefix, logging=LoggingConfig(file_level=LogLevel.TRACE))
    merged = base.merge_with(override)
    assert merged.logging.file_level == LogLevel.TRACE


# =============================================================================
# Tests for CommandDefaults.merge_with
# =============================================================================


def test_command_defaults_merge_with_combines_defaults() -> None:
    """CommandDefaults.merge_with should combine defaults from both configs."""
    base = CommandDefaults(defaults={"name": "base", "other": "base_value"})
    override = CommandDefaults(defaults={"name": "override"})
    merged = base.merge_with(override)
    assert merged.defaults["name"] == "override"
    assert merged.defaults["other"] == "base_value"


def test_command_defaults_merge_with_override_wins_for_default_subcommand() -> None:
    """CommandDefaults.merge_with should let override win for default_subcommand."""
    base = CommandDefaults(default_subcommand="create")
    override = CommandDefaults(default_subcommand="list")
    merged = base.merge_with(override)
    assert merged.default_subcommand == "list"


def test_command_defaults_merge_with_keeps_base_default_subcommand_when_override_none() -> None:
    """CommandDefaults.merge_with should keep base default_subcommand when override is None."""
    base = CommandDefaults(default_subcommand="create")
    override = CommandDefaults()
    merged = base.merge_with(override)
    assert merged.default_subcommand == "create"


def test_command_defaults_merge_with_empty_string_default_subcommand() -> None:
    """CommandDefaults.merge_with should allow empty string to disable defaulting."""
    base = CommandDefaults(default_subcommand="create")
    override = CommandDefaults(default_subcommand="")
    merged = base.merge_with(override)
    assert merged.default_subcommand == ""


def test_command_defaults_merge_with_default_subcommand_independent_of_defaults() -> None:
    """CommandDefaults.merge_with should merge default_subcommand and defaults independently."""
    base = CommandDefaults(defaults={"host": "local"}, default_subcommand="create")
    override = CommandDefaults(defaults={"host": "docker"}, default_subcommand="list")
    merged = base.merge_with(override)
    assert merged.defaults["host"] == "docker"
    assert merged.default_subcommand == "list"


# =============================================================================
# Tests for CreateTemplate.merge_with
# =============================================================================


def test_create_template_merge_with_combines_options() -> None:
    """CreateTemplate.merge_with should combine options from both templates."""
    base = CreateTemplate(options={"new_host": "local", "target_path": "/base"})
    override = CreateTemplate(options={"new_host": "docker"})
    merged = base.merge_with(override)
    assert merged.options["new_host"] == "docker"
    assert merged.options["target_path"] == "/base"


def test_create_template_merge_with_override_wins_for_same_key() -> None:
    """CreateTemplate.merge_with should let override win for same keys."""
    base = CreateTemplate(options={"connect": True, "await_ready": True})
    override = CreateTemplate(options={"connect": False})
    merged = base.merge_with(override)
    assert merged.options["connect"] is False
    assert merged.options["await_ready"] is True


def test_create_template_merge_with_empty_base() -> None:
    """CreateTemplate.merge_with should handle empty base template."""
    base = CreateTemplate()
    override = CreateTemplate(options={"new_host": "docker"})
    merged = base.merge_with(override)
    assert merged.options["new_host"] == "docker"


def test_create_template_merge_with_empty_override() -> None:
    """CreateTemplate.merge_with should handle empty override template."""
    base = CreateTemplate(options={"new_host": "local"})
    override = CreateTemplate()
    merged = base.merge_with(override)
    assert merged.options["new_host"] == "local"


# =============================================================================
# Tests for MngConfig.create_templates
# =============================================================================


def test_mng_config_merge_with_merges_create_templates(mng_test_prefix: str) -> None:
    """MngConfig.merge_with should merge create_templates with same key."""
    base = MngConfig(
        prefix=mng_test_prefix,
        create_templates={
            CreateTemplateName("modal"): CreateTemplate(options={"new_host": "modal", "target_path": "/base"}),
        },
    )
    override = MngConfig(
        prefix=mng_test_prefix,
        create_templates={
            CreateTemplateName("modal"): CreateTemplate(options={"target_path": "/override"}),
        },
    )
    merged = base.merge_with(override)
    modal_template = merged.create_templates[CreateTemplateName("modal")]
    assert modal_template.options["new_host"] == "modal"
    assert modal_template.options["target_path"] == "/override"


def test_mng_config_merge_with_adds_new_create_templates(mng_test_prefix: str) -> None:
    """MngConfig.merge_with should add new create_templates from override."""
    base = MngConfig(
        prefix=mng_test_prefix,
        create_templates={CreateTemplateName("modal"): CreateTemplate(options={"new_host": "modal"})},
    )
    override = MngConfig(
        prefix=mng_test_prefix,
        create_templates={CreateTemplateName("docker"): CreateTemplate(options={"new_host": "docker"})},
    )
    merged = base.merge_with(override)
    assert CreateTemplateName("modal") in merged.create_templates
    assert CreateTemplateName("docker") in merged.create_templates


def test_mng_config_create_templates_default_is_empty_dict(mng_test_prefix: str) -> None:
    """MngConfig should have empty create_templates by default."""
    config = MngConfig(prefix=mng_test_prefix)
    assert config.create_templates == {}


# =============================================================================
# Tests for MngConfig.pre_command_scripts
# =============================================================================


def test_mng_config_merge_with_merges_pre_command_scripts(mng_test_prefix: str) -> None:
    """MngConfig.merge_with should merge pre_command_scripts dicts."""
    base = MngConfig(
        prefix=mng_test_prefix,
        pre_command_scripts={"create": ["echo base"], "list": ["echo list"]},
    )
    override = MngConfig(
        prefix=mng_test_prefix,
        pre_command_scripts={"create": ["echo override"]},
    )
    merged = base.merge_with(override)
    assert merged.pre_command_scripts["create"] == ["echo override"]
    assert merged.pre_command_scripts["list"] == ["echo list"]


def test_mng_config_merge_with_adds_new_pre_command_scripts(mng_test_prefix: str) -> None:
    """MngConfig.merge_with should add new pre_command_scripts from override."""
    base = MngConfig(
        prefix=mng_test_prefix,
        pre_command_scripts={"create": ["echo create"]},
    )
    override = MngConfig(
        prefix=mng_test_prefix,
        pre_command_scripts={"destroy": ["echo destroy"]},
    )
    merged = base.merge_with(override)
    assert "create" in merged.pre_command_scripts
    assert "destroy" in merged.pre_command_scripts


def test_mng_config_pre_command_scripts_default_is_empty_dict(mng_test_prefix: str) -> None:
    """MngConfig should have empty pre_command_scripts by default."""
    config = MngConfig(prefix=mng_test_prefix)
    assert config.pre_command_scripts == {}


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


def test_provider_instance_config_merge_preserves_is_enabled_false() -> None:
    """ProviderInstanceConfig merge should preserve is_enabled when set to False in override."""
    base = ProviderInstanceConfig(backend=ProviderBackendName("local"), is_enabled=True)
    override = ProviderInstanceConfig(backend=ProviderBackendName("local"), is_enabled=False)
    merged = base.merge_with(override)
    assert merged.is_enabled is False


# =============================================================================
# Tests for MngConfig.enabled_backends
# =============================================================================


def test_mng_config_enabled_backends_default_empty(mng_test_prefix: str) -> None:
    """MngConfig.enabled_backends should default to empty list (all backends enabled)."""
    config = MngConfig(prefix=mng_test_prefix)
    assert config.enabled_backends == []


def test_mng_config_enabled_backends_can_be_set(mng_test_prefix: str) -> None:
    """MngConfig.enabled_backends can be set to specific backends."""
    config = MngConfig(
        prefix=mng_test_prefix,
        enabled_backends=[ProviderBackendName("local"), ProviderBackendName("docker")],
    )
    assert ProviderBackendName("local") in config.enabled_backends
    assert ProviderBackendName("docker") in config.enabled_backends


def test_mng_config_merge_enabled_backends_override_wins_when_not_empty(mng_test_prefix: str) -> None:
    """MngConfig merge should use override's enabled_backends when it's not empty."""
    base = MngConfig(
        prefix=mng_test_prefix,
        enabled_backends=[ProviderBackendName("local"), ProviderBackendName("docker")],
    )
    override = MngConfig(
        prefix=mng_test_prefix,
        enabled_backends=[ProviderBackendName("modal")],
    )
    merged = base.merge_with(override)
    assert merged.enabled_backends == [ProviderBackendName("modal")]


def test_mng_config_merge_enabled_backends_keeps_base_when_override_empty(mng_test_prefix: str) -> None:
    """MngConfig merge should keep base's enabled_backends when override's is empty."""
    base = MngConfig(
        prefix=mng_test_prefix,
        enabled_backends=[ProviderBackendName("local")],
    )
    override = MngConfig(
        prefix=mng_test_prefix,
        enabled_backends=[],
    )
    merged = base.merge_with(override)
    assert merged.enabled_backends == [ProviderBackendName("local")]


# =============================================================================
# Tests for MngConfig.is_remote_agent_installation_allowed
# =============================================================================


def test_mng_config_merge_is_remote_agent_installation_allowed_override_wins(mng_test_prefix: str) -> None:
    """MngConfig merge should use override's is_remote_agent_installation_allowed when set."""
    base = MngConfig(prefix=mng_test_prefix, is_remote_agent_installation_allowed=True)
    override = MngConfig(prefix=mng_test_prefix, is_remote_agent_installation_allowed=False)
    merged = base.merge_with(override)
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
    cli_args_str = """--settings '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "./scripts/check_commit_status.sh"}]}, {"hooks": [{"type": "command", "timeout": 600, "command": "./scripts/main_claude_stop_hook.sh"}]}]}}'"""
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


def test_provider_instance_config_merge_overrides_destroyed_host_persisted_seconds() -> None:
    base = ProviderInstanceConfig(
        backend=ProviderBackendName("local"),
        destroyed_host_persisted_seconds=3600.0,
    )
    override = ProviderInstanceConfig(
        backend=ProviderBackendName("local"),
        destroyed_host_persisted_seconds=7200.0,
    )
    merged = base.merge_with(override)
    assert merged.destroyed_host_persisted_seconds == 7200.0


def test_provider_instance_config_merge_keeps_base_when_override_is_none() -> None:
    base = ProviderInstanceConfig(
        backend=ProviderBackendName("local"),
        destroyed_host_persisted_seconds=3600.0,
    )
    override = ProviderInstanceConfig.model_construct(
        backend=ProviderBackendName("local"),
        destroyed_host_persisted_seconds=None,
    )
    merged = base.merge_with(override)
    assert merged.destroyed_host_persisted_seconds == 3600.0


def test_mng_config_default_destroyed_host_persisted_seconds_is_seven_days(mng_test_prefix: str) -> None:
    config = MngConfig(prefix=mng_test_prefix)
    assert config.default_destroyed_host_persisted_seconds == 60.0 * 60.0 * 24.0 * 7.0


def test_mng_config_merge_overrides_default_destroyed_host_persisted_seconds(mng_test_prefix: str) -> None:
    base = MngConfig(prefix=mng_test_prefix, default_destroyed_host_persisted_seconds=604800.0)
    override = MngConfig(prefix=mng_test_prefix, default_destroyed_host_persisted_seconds=86400.0)
    merged = base.merge_with(override)
    assert merged.default_destroyed_host_persisted_seconds == 86400.0


def test_mng_config_merge_keeps_base_destroyed_host_persisted_seconds_when_override_none(
    mng_test_prefix: str,
) -> None:
    base = MngConfig(prefix=mng_test_prefix, default_destroyed_host_persisted_seconds=86400.0)
    override = MngConfig.model_construct(
        prefix=mng_test_prefix,
        default_destroyed_host_persisted_seconds=None,
    )
    merged = base.merge_with(override)
    assert merged.default_destroyed_host_persisted_seconds == 86400.0


def test_mng_config_merge_overrides_connect_command(mng_test_prefix: str) -> None:
    base = MngConfig(prefix=mng_test_prefix, connect_command="base-cmd")
    override = MngConfig(prefix=mng_test_prefix, connect_command="override-cmd")
    merged = base.merge_with(override)
    assert merged.connect_command == "override-cmd"


def test_mng_config_merge_keeps_base_connect_command_when_override_none(mng_test_prefix: str) -> None:
    base = MngConfig(prefix=mng_test_prefix, connect_command="base-cmd")
    override = MngConfig.model_construct(prefix=mng_test_prefix, connect_command=None)
    merged = base.merge_with(override)
    assert merged.connect_command == "base-cmd"


def test_mng_config_connect_command_defaults_to_none(mng_test_prefix: str) -> None:
    config = MngConfig(prefix=mng_test_prefix)
    assert config.connect_command is None
