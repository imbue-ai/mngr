"""Tests for config loader."""

from collections.abc import Generator
from pathlib import Path
from typing import Any

import click
import pluggy
import pytest
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.config.data_types import CommandDefaults
from imbue.mng.config.data_types import CreateTemplateName
from imbue.mng.config.data_types import PluginConfig
from imbue.mng.config.data_types import get_or_create_user_id
from imbue.mng.config.loader import _apply_plugin_overrides
from imbue.mng.config.loader import _merge_command_defaults
from imbue.mng.config.loader import _normalize_cli_args_for_construct
from imbue.mng.config.loader import _parse_agent_types
from imbue.mng.config.loader import _parse_command_env_vars
from imbue.mng.config.loader import _parse_commands
from imbue.mng.config.loader import _parse_create_templates
from imbue.mng.config.loader import _parse_logging_config
from imbue.mng.config.loader import _parse_plugins
from imbue.mng.config.loader import _parse_providers
from imbue.mng.config.loader import block_disabled_plugins
from imbue.mng.config.loader import get_or_create_profile_dir
from imbue.mng.config.loader import load_config
from imbue.mng.config.loader import parse_config
from imbue.mng.errors import ConfigParseError
from imbue.mng.main import cli
from imbue.mng.plugins import hookspecs
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import LogLevel
from imbue.mng.primitives import PluginName
from imbue.mng.primitives import ProviderBackendName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.registry import load_all_registries
from imbue.mng.utils.logging import LoggingConfig

hookimpl = pluggy.HookimplMarker("mng")


@pytest.fixture()
def log_warnings() -> Generator[list[str], None, None]:
    """Capture loguru warning messages for assertion in tests."""
    messages: list[str] = []
    handler_id = logger.add(lambda msg: messages.append(msg.record["message"]), level="WARNING", format="{message}")
    yield messages
    logger.remove(handler_id)


# =============================================================================
# Tests for _parse_command_env_vars
# =============================================================================


def test_parse_command_env_vars_single_param() -> None:
    """Test parsing a single command param from env var."""
    environ = {"MNG_COMMANDS_CREATE_NEW_BRANCH_PREFIX": "agent/"}
    result = _parse_command_env_vars(environ)

    assert "create" in result
    assert result["create"].defaults["new_branch_prefix"] == "agent/"


def test_parse_command_env_vars_multiple_params_same_command() -> None:
    """Test parsing multiple params for the same command."""
    environ = {
        "MNG_COMMANDS_CREATE_NEW_BRANCH_PREFIX": "agent/",
        "MNG_COMMANDS_CREATE_CONNECT": "false",
    }
    result = _parse_command_env_vars(environ)

    assert "create" in result
    assert result["create"].defaults["new_branch_prefix"] == "agent/"
    # Values are kept as strings - type conversion happens in click/pydantic
    assert result["create"].defaults["connect"] == "false"


def test_parse_command_env_vars_multiple_commands() -> None:
    """Test parsing params for different commands."""
    environ = {
        "MNG_COMMANDS_CREATE_NAME": "myagent",
        "MNG_COMMANDS_LIST_FORMAT": "json",
    }
    result = _parse_command_env_vars(environ)

    assert "create" in result
    assert result["create"].defaults["name"] == "myagent"
    assert "list" in result
    assert result["list"].defaults["format"] == "json"


def test_parse_command_env_vars_ignores_non_matching_vars() -> None:
    """Test that non-matching env vars are ignored."""
    environ = {
        "MNG_COMMANDS_CREATE_NAME": "myagent",
        "MNG_PREFIX": "test-",
        "PATH": "/usr/bin",
        "HOME": "/home/user",
    }
    result = _parse_command_env_vars(environ)

    assert "create" in result
    assert len(result) == 1


def test_parse_command_env_vars_ignores_no_underscore_after_command() -> None:
    """Test that vars without underscore after command prefix are ignored."""
    environ = {"MNG_COMMANDS_CREATE": "ignored"}
    result = _parse_command_env_vars(environ)

    assert len(result) == 0


def test_parse_command_env_vars_lowercases_command_and_param() -> None:
    """Test that command and param names are lowercased."""
    environ = {"MNG_COMMANDS_CREATE_NEW_BRANCH_PREFIX": "agent/"}
    result = _parse_command_env_vars(environ)

    assert "create" in result
    assert "new_branch_prefix" in result["create"].defaults


def test_parse_command_env_vars_empty_environ() -> None:
    """Test parsing empty environ returns empty dict."""
    result = _parse_command_env_vars({})
    assert result == {}


def test_parse_command_env_vars_preserves_values_as_strings() -> None:
    """Test that all values are preserved as strings.

    Type conversion happens downstream in click/pydantic where the
    actual type information is available.
    """
    environ = {
        "MNG_COMMANDS_CREATE_CONNECT": "true",
        "MNG_COMMANDS_CREATE_RETRY": "5",
        "MNG_COMMANDS_CREATE_NAME": "myagent",
    }
    result = _parse_command_env_vars(environ)

    # All values should be strings
    assert result["create"].defaults["connect"] == "true"
    assert result["create"].defaults["retry"] == "5"
    assert result["create"].defaults["name"] == "myagent"
    assert all(isinstance(v, str) for v in result["create"].defaults.values())


# =============================================================================
# Tests for _merge_command_defaults
# =============================================================================


def test_merge_command_defaults_empty_base() -> None:
    """Test merging into empty base."""
    base: dict[str, CommandDefaults] = {}
    override = {"create": CommandDefaults(defaults={"name": "test"})}
    result = _merge_command_defaults(base, override)

    assert "create" in result
    assert result["create"].defaults["name"] == "test"


def test_merge_command_defaults_empty_override() -> None:
    """Test merging empty override."""
    base = {"create": CommandDefaults(defaults={"name": "test"})}
    override: dict[str, CommandDefaults] = {}
    result = _merge_command_defaults(base, override)

    assert "create" in result
    assert result["create"].defaults["name"] == "test"


def test_merge_command_defaults_combines_different_commands() -> None:
    """Test merging with different commands."""
    base = {"create": CommandDefaults(defaults={"name": "test"})}
    override = {"list": CommandDefaults(defaults={"format": "json"})}
    result = _merge_command_defaults(base, override)

    assert "create" in result
    assert "list" in result


def test_merge_command_defaults_override_wins_same_command() -> None:
    """Test that override wins for same command params."""
    base = {"create": CommandDefaults(defaults={"name": "old", "other": "base"})}
    override = {"create": CommandDefaults(defaults={"name": "new"})}
    result = _merge_command_defaults(base, override)

    assert result["create"].defaults["name"] == "new"
    assert result["create"].defaults["other"] == "base"


# =============================================================================
# Test for single-word command names
# =============================================================================


def test_all_cli_commands_are_single_word() -> None:
    """Ensure all CLI command names are single words (no spaces, hyphens, or underscores).

    This is CRITICAL for the MNG_COMMANDS_<COMMANDNAME>_<PARAMNAME> env var parsing
    to work correctly. If command names contained underscores, parsing would be ambiguous.

    For example, if a command was named "foo_bar" and a param was "baz", the env var
    would be "MNG_COMMANDS_FOO_BAR_BAZ", which could be interpreted as either:
        - command="foo", param="bar_baz"
        - command="foo_bar", param="baz"

    By requiring single-word commands, we avoid this ambiguity.

    Any future plugins that register custom commands MUST also follow this convention.
    """
    # Get all commands from the CLI group
    assert isinstance(cli, click.Group), "cli should be a click.Group"

    invalid_commands = []
    for command_name in cli.commands.keys():
        # Check for spaces, hyphens, or underscores in command names
        if " " in command_name or "-" in command_name or "_" in command_name:
            invalid_commands.append(command_name)

    assert not invalid_commands, (
        f"CLI command names must be single words (no spaces, hyphens, or underscores) "
        f"for MNG_COMMANDS_<COMMANDNAME>_<PARAMNAME> env var parsing to work. "
        f"Invalid commands: {invalid_commands}"
    )


# =============================================================================
# Tests for _parse_providers
# =============================================================================


def test_parse_providers_parses_valid_provider() -> None:
    """_parse_providers should parse valid provider configs."""
    raw = {"my-local": {"backend": "local"}}
    result = _parse_providers(raw, disabled_plugins=frozenset())
    assert ProviderInstanceName("my-local") in result
    assert result[ProviderInstanceName("my-local")].backend == ProviderBackendName("local")


def test_parse_providers_raises_on_unknown_backend() -> None:
    """_parse_providers should raise ConfigParseError for unknown backend."""
    raw = {"my-provider": {"some_field": "value"}}
    with pytest.raises(ConfigParseError, match="references unknown backend 'my-provider'"):
        _parse_providers(raw, disabled_plugins=frozenset())


def test_parse_providers_raises_on_unknown_fields() -> None:
    """_parse_providers should raise ConfigParseError for unknown fields by default."""
    raw = {"my-local": {"backend": "local", "typo_field": "value"}}
    with pytest.raises(ConfigParseError, match="Unknown fields in providers.my-local.*typo_field"):
        _parse_providers(raw, disabled_plugins=frozenset())


def test_parse_providers_warns_on_unknown_fields_when_not_strict(log_warnings: list[str]) -> None:
    """_parse_providers with strict=False should warn about unknown fields and strip them."""
    raw = {"my-local": {"backend": "local", "typo_field": "value"}}
    result = _parse_providers(raw, disabled_plugins=frozenset(), strict=False)
    assert ProviderInstanceName("my-local") in result
    assert "typo_field" not in raw["my-local"]
    assert any("typo_field" in msg and "providers.my-local" in msg for msg in log_warnings)


def test_parse_providers_skips_disabled_plugin() -> None:
    """_parse_providers should skip provider blocks whose plugin is disabled."""
    raw = {"modal": {"backend": "modal"}}
    result = _parse_providers(raw, disabled_plugins=frozenset({"modal"}))
    assert len(result) == 0


def test_parse_providers_keeps_non_disabled_providers() -> None:
    """_parse_providers should parse providers whose plugin is not disabled."""
    raw = {
        "my-local": {"backend": "local"},
        "modal": {"backend": "modal"},
    }
    result = _parse_providers(raw, disabled_plugins=frozenset({"modal"}))
    assert ProviderInstanceName("my-local") in result
    assert ProviderInstanceName("modal") not in result


def test_parse_providers_explicit_plugin_field_overrides_backend_for_skip() -> None:
    """_parse_providers should use explicit plugin field for disabled-plugin check."""
    raw = {"my-cloud": {"backend": "local", "plugin": "my-cloud-plugin"}}
    result = _parse_providers(raw, disabled_plugins=frozenset({"my-cloud-plugin"}))
    assert len(result) == 0


def test_parse_providers_explicit_plugin_field_not_disabled() -> None:
    """_parse_providers should parse provider when explicit plugin is not disabled."""
    raw = {"my-local": {"backend": "local", "plugin": "some-plugin"}}
    result = _parse_providers(raw, disabled_plugins=frozenset({"other-plugin"}))
    assert ProviderInstanceName("my-local") in result


def test_parse_providers_unknown_backend_mentions_disabled_plugins() -> None:
    """_parse_providers error message should mention disabled plugins when they exist."""
    raw = {"my-provider": {"backend": "nonexistent"}}
    with pytest.raises(ConfigParseError, match="Currently disabled plugins: modal"):
        _parse_providers(raw, disabled_plugins=frozenset({"modal"}))


# =============================================================================
# Tests for _parse_agent_types
# =============================================================================


def test_parse_agent_types_parses_valid_agent() -> None:
    """_parse_agent_types should parse valid agent type configs."""
    raw = {"claude": {"cli_args": "--verbose"}}
    result = _parse_agent_types(raw)
    assert AgentTypeName("claude") in result
    assert result[AgentTypeName("claude")].cli_args == ("--verbose",)


def test_parse_agent_types_handles_empty_dict() -> None:
    """_parse_agent_types should handle empty dict."""
    result = _parse_agent_types({})
    assert result == {}


def test_parse_agent_types_raises_on_unknown_fields() -> None:
    """_parse_agent_types should raise ConfigParseError for unknown fields by default."""
    raw = {"claude": {"cli_args": "--verbose", "bogus_option": True}}
    with pytest.raises(ConfigParseError, match="Unknown fields in agent_types.claude.*bogus_option"):
        _parse_agent_types(raw)


def test_parse_agent_types_warns_on_unknown_fields_when_not_strict(log_warnings: list[str]) -> None:
    """_parse_agent_types with strict=False should warn about unknown fields and strip them."""
    raw = {"claude": {"cli_args": "--verbose", "bogus_option": True}}
    result = _parse_agent_types(raw, strict=False)
    assert AgentTypeName("claude") in result
    assert result[AgentTypeName("claude")].cli_args == ("--verbose",)
    assert "bogus_option" not in raw["claude"]
    assert any("bogus_option" in msg and "agent_types.claude" in msg for msg in log_warnings)


# =============================================================================
# Tests for _parse_plugins
# =============================================================================


def test_parse_plugins_parses_valid_plugin() -> None:
    """_parse_plugins should parse valid plugin configs."""
    raw = {"my-plugin": {"enabled": True}}
    result = _parse_plugins(raw)
    assert PluginName("my-plugin") in result
    assert result[PluginName("my-plugin")].enabled is True


def test_parse_plugins_handles_empty_dict() -> None:
    """_parse_plugins should handle empty dict."""
    result = _parse_plugins({})
    assert result == {}


def test_parse_plugins_raises_on_unknown_fields() -> None:
    """_parse_plugins should raise ConfigParseError for unknown fields by default."""
    raw = {"my-plugin": {"enabled": True, "nonexistent_setting": "abc"}}
    with pytest.raises(ConfigParseError, match="Unknown fields in plugins.my-plugin.*nonexistent_setting"):
        _parse_plugins(raw)


def test_parse_plugins_warns_on_unknown_fields_when_not_strict(log_warnings: list[str]) -> None:
    """_parse_plugins with strict=False should warn about unknown fields and strip them."""
    raw = {"my-plugin": {"enabled": True, "nonexistent_setting": "abc"}}
    result = _parse_plugins(raw, strict=False)
    assert PluginName("my-plugin") in result
    assert result[PluginName("my-plugin")].enabled is True
    assert "nonexistent_setting" not in raw["my-plugin"]
    assert any("nonexistent_setting" in msg and "plugins.my-plugin" in msg for msg in log_warnings)


# =============================================================================
# Tests for _apply_plugin_overrides
# =============================================================================


def test_apply_plugin_overrides_enables_plugins() -> None:
    """_apply_plugin_overrides should enable plugins."""
    plugins: dict[PluginName, PluginConfig] = {}
    result, disabled = _apply_plugin_overrides(plugins, enabled_plugins=["my-plugin"], disabled_plugins=None)
    assert PluginName("my-plugin") in result
    assert result[PluginName("my-plugin")].enabled is True
    assert len(disabled) == 0


def test_apply_plugin_overrides_disables_plugins() -> None:
    """_apply_plugin_overrides should disable and filter out plugins."""
    plugins = {PluginName("my-plugin"): PluginConfig(enabled=True)}
    result, disabled = _apply_plugin_overrides(plugins, enabled_plugins=None, disabled_plugins=["my-plugin"])
    # Disabled plugins are filtered out
    assert PluginName("my-plugin") not in result
    assert "my-plugin" in disabled


def test_apply_plugin_overrides_filters_disabled_plugins() -> None:
    """_apply_plugin_overrides should filter out disabled plugins."""
    plugins = {
        PluginName("enabled-plugin"): PluginConfig(enabled=True),
        PluginName("disabled-plugin"): PluginConfig(enabled=False),
    }
    result, disabled = _apply_plugin_overrides(plugins, enabled_plugins=None, disabled_plugins=None)
    assert PluginName("enabled-plugin") in result
    assert PluginName("disabled-plugin") not in result
    assert "disabled-plugin" in disabled


def test_apply_plugin_overrides_enables_existing_plugin() -> None:
    """_apply_plugin_overrides should enable existing disabled plugins."""
    plugins = {PluginName("my-plugin"): PluginConfig(enabled=False)}
    result, disabled = _apply_plugin_overrides(plugins, enabled_plugins=["my-plugin"], disabled_plugins=None)
    assert PluginName("my-plugin") in result
    assert result[PluginName("my-plugin")].enabled is True
    assert "my-plugin" not in disabled


def test_apply_plugin_overrides_creates_disabled_plugin() -> None:
    """_apply_plugin_overrides should create new disabled plugins."""
    plugins: dict[PluginName, PluginConfig] = {}
    result, disabled = _apply_plugin_overrides(plugins, enabled_plugins=None, disabled_plugins=["new-plugin"])
    # Disabled plugins are filtered out, so should not be in result
    assert PluginName("new-plugin") not in result
    assert "new-plugin" in disabled


# =============================================================================
# Tests for _parse_logging_config
# =============================================================================


def test_parse_logging_config_parses_valid_config() -> None:
    """_parse_logging_config should parse valid logging config."""
    raw = {"file_level": "TRACE", "max_log_size_mb": 20}
    result = _parse_logging_config(raw)
    assert isinstance(result, LoggingConfig)
    assert result.file_level == LogLevel.TRACE
    assert result.max_log_size_mb == 20


def test_parse_logging_config_handles_empty_dict() -> None:
    """_parse_logging_config should handle empty dict."""
    result = _parse_logging_config({})
    assert isinstance(result, LoggingConfig)


def test_parse_logging_config_raises_on_unknown_fields() -> None:
    """_parse_logging_config should raise ConfigParseError for unknown fields by default."""
    raw = {"file_level": "DEBUG", "unknown_log_option": 42}
    with pytest.raises(ConfigParseError, match="Unknown fields in logging.*unknown_log_option"):
        _parse_logging_config(raw)


def test_parse_logging_config_warns_on_unknown_fields_when_not_strict(log_warnings: list[str]) -> None:
    """_parse_logging_config with strict=False should warn about unknown fields and strip them."""
    raw = {"file_level": "DEBUG", "unknown_log_option": 42}
    result = _parse_logging_config(raw, strict=False)
    assert isinstance(result, LoggingConfig)
    assert "unknown_log_option" not in raw
    assert any("unknown_log_option" in msg for msg in log_warnings)


# =============================================================================
# Tests for _parse_commands
# =============================================================================


def test_parse_commands_parses_valid_commands() -> None:
    """_parse_commands should parse valid command defaults."""
    raw = {"create": {"name": "test-agent", "connect": False}}
    result = _parse_commands(raw)
    assert "create" in result
    assert result["create"].defaults["name"] == "test-agent"
    assert result["create"].defaults["connect"] is False


def test_parse_commands_handles_empty_dict() -> None:
    """_parse_commands should handle empty dict."""
    result = _parse_commands({})
    assert result == {}


# =============================================================================
# Tests for _parse_create_templates
# =============================================================================


def test_parse_create_templates_parses_valid_templates() -> None:
    """_parse_create_templates should parse valid create templates."""
    raw = {"modal-dev": {"new_host": "modal", "target_path": "/root/workspace"}}
    result = _parse_create_templates(raw)
    assert CreateTemplateName("modal-dev") in result
    assert result[CreateTemplateName("modal-dev")].options["new_host"] == "modal"
    assert result[CreateTemplateName("modal-dev")].options["target_path"] == "/root/workspace"


def test_parse_create_templates_handles_empty_dict() -> None:
    """_parse_create_templates should handle empty dict."""
    result = _parse_create_templates({})
    assert result == {}


def test_parse_create_templates_multiple_templates() -> None:
    """_parse_create_templates should parse multiple templates."""
    raw = {
        "modal": {"new_host": "modal"},
        "docker": {"new_host": "docker"},
        "local": {"in_place": True},
    }
    result = _parse_create_templates(raw)
    assert len(result) == 3
    assert CreateTemplateName("modal") in result
    assert CreateTemplateName("docker") in result
    assert CreateTemplateName("local") in result


# =============================================================================
# Tests for parse_config
# =============================================================================


def test_parse_config_parses_full_config() -> None:
    """parse_config should parse a full config dict."""
    raw = {
        "prefix": "test-",
        "default_host_dir": "/tmp/test",
        "agent_types": {"claude": {"cli_args": "--verbose"}},
        "providers": {"local": {"backend": "local"}},
        "plugins": {"my-plugin": {"enabled": True}},
        "commands": {"create": {"name": "test"}},
        "create_templates": {"modal": {"new_host": "modal"}},
        "logging": {"file_level": "DEBUG"},
    }
    result = parse_config(raw, disabled_plugins=frozenset())
    assert result.prefix == "test-"
    assert result.default_host_dir == "/tmp/test"
    assert AgentTypeName("claude") in result.agent_types
    assert ProviderInstanceName("local") in result.providers
    assert PluginName("my-plugin") in result.plugins
    assert "create" in result.commands
    assert CreateTemplateName("modal") in result.create_templates
    assert result.logging is not None


def test_parse_config_handles_minimal_config() -> None:
    """parse_config should handle minimal config with missing optional fields."""
    raw = {"prefix": "test-"}
    result = parse_config(raw, disabled_plugins=frozenset())
    assert result.prefix == "test-"
    assert result.agent_types == {}
    assert result.providers == {}
    assert result.plugins == {}
    assert result.commands == {}
    assert result.logging is None


def test_parse_config_handles_empty_config() -> None:
    """parse_config should handle empty config dict."""
    result = parse_config({}, disabled_plugins=frozenset())
    assert result.prefix is None
    assert result.default_host_dir is None
    assert result.agent_types == {}
    assert result.providers == {}
    assert result.plugins == {}
    assert result.commands == {}
    assert result.logging is None


def test_parse_config_raises_on_unknown_top_level_field() -> None:
    """parse_config should raise ConfigParseError for unknown top-level fields by default."""
    raw = {"prefix": "test-", "nonexistent_top_level": "value"}
    with pytest.raises(ConfigParseError, match="Unknown configuration fields.*nonexistent_top_level"):
        parse_config(raw, disabled_plugins=frozenset())


def test_parse_config_warns_on_unknown_top_level_field_when_not_strict(log_warnings: list[str]) -> None:
    """parse_config with strict=False should warn about unknown top-level fields."""
    raw = {"prefix": "test-", "nonexistent_top_level": "value"}
    result = parse_config(raw, disabled_plugins=frozenset(), strict=False)
    assert result.prefix == "test-"
    assert any("nonexistent_top_level" in msg for msg in log_warnings)


def test_parse_config_raises_on_unknown_nested_field() -> None:
    """parse_config should raise ConfigParseError for unknown nested fields by default."""
    raw = {
        "logging": {"file_level": "DEBUG", "bad_field": True},
    }
    with pytest.raises(ConfigParseError, match="Unknown fields in logging.*bad_field"):
        parse_config(raw, disabled_plugins=frozenset())


def test_parse_config_warns_on_unknown_nested_field_when_not_strict(log_warnings: list[str]) -> None:
    """parse_config with strict=False should warn about unknown nested fields."""
    raw = {
        "logging": {"file_level": "DEBUG", "bad_field": True},
    }
    result = parse_config(raw, disabled_plugins=frozenset(), strict=False)
    assert result.logging is not None
    assert any("bad_field" in msg for msg in log_warnings)


def test_parse_config_parses_default_destroyed_host_persisted_seconds() -> None:
    """parse_config should parse default_destroyed_host_persisted_seconds from config."""
    raw = {"default_destroyed_host_persisted_seconds": 86400.0}
    result = parse_config(raw, disabled_plugins=frozenset())
    assert result.default_destroyed_host_persisted_seconds == 86400.0


def test_parse_config_handles_missing_default_destroyed_host_persisted_seconds() -> None:
    """parse_config should set None when default_destroyed_host_persisted_seconds is absent."""
    result = parse_config({}, disabled_plugins=frozenset())
    assert result.default_destroyed_host_persisted_seconds is None


def test_parse_providers_accepts_destroyed_host_persisted_seconds() -> None:
    """_parse_providers should accept destroyed_host_persisted_seconds on any provider config."""
    raw_providers = {
        "my-local": {
            "backend": "local",
            "destroyed_host_persisted_seconds": 172800.0,
        },
    }
    result = _parse_providers(raw_providers, disabled_plugins=frozenset())
    provider_config = result[ProviderInstanceName("my-local")]
    assert provider_config.destroyed_host_persisted_seconds == 172800.0


# =============================================================================
# Tests for on_load_config hook
# =============================================================================


def test_on_load_config_hook_is_called(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Test that the on_load_config hook is called during load_config."""
    # Track whether hook was called
    hook_called = False
    received_config_dict: dict[str, Any] = {}

    class TestPlugin:
        @hookimpl
        def on_load_config(self, config_dict: dict[str, Any]) -> None:
            nonlocal hook_called, received_config_dict
            hook_called = True
            received_config_dict = dict(config_dict)

    # Set up plugin manager with our test plugin
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)
    pm.register(TestPlugin())
    load_all_registries(pm)

    # Ensure no config files interfere
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MNG_PREFIX", raising=False)
    monkeypatch.delenv("MNG_HOST_DIR", raising=False)
    monkeypatch.delenv("MNG_ROOT_NAME", raising=False)

    # Call load_config
    load_config(
        pm=pm,
        concurrency_group=cg,
        context_dir=tmp_path,
    )

    # Verify hook was called
    assert hook_called, "on_load_config hook was not called"
    assert "prefix" in received_config_dict or "providers" in received_config_dict


def test_on_load_config_hook_can_modify_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cg: ConcurrencyGroup
) -> None:
    """Test that on_load_config hook can modify the config dict."""

    class TestPlugin:
        @hookimpl
        def on_load_config(self, config_dict: dict[str, Any]) -> None:
            # Modify the config dict to change the prefix
            config_dict["prefix"] = "modified-by-plugin-"

    # Set up plugin manager with our test plugin
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)
    pm.register(TestPlugin())
    load_all_registries(pm)

    # Ensure no config files interfere
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MNG_PREFIX", raising=False)
    monkeypatch.delenv("MNG_HOST_DIR", raising=False)
    monkeypatch.delenv("MNG_ROOT_NAME", raising=False)

    # Call load_config
    mng_ctx = load_config(
        pm=pm,
        concurrency_group=cg,
        context_dir=tmp_path,
    )

    # Verify the config was modified
    assert mng_ctx.config.prefix == "modified-by-plugin-"


def test_on_load_config_hook_can_add_new_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cg: ConcurrencyGroup
) -> None:
    """Test that on_load_config hook can add new config fields."""

    class TestPlugin:
        @hookimpl
        def on_load_config(self, config_dict: dict[str, Any]) -> None:
            # Add a custom agent type
            if "agent_types" not in config_dict:
                config_dict["agent_types"] = {}
            config_dict["agent_types"][AgentTypeName("custom-agent")] = {"cli_args": "--custom"}

    # Set up plugin manager with our test plugin
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)
    pm.register(TestPlugin())
    load_all_registries(pm)

    # Ensure no config files interfere
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MNG_PREFIX", raising=False)
    monkeypatch.delenv("MNG_HOST_DIR", raising=False)
    monkeypatch.delenv("MNG_ROOT_NAME", raising=False)

    # Call load_config
    mng_ctx = load_config(
        pm=pm,
        concurrency_group=cg,
        context_dir=tmp_path,
    )

    # Verify the agent type was added
    assert AgentTypeName("custom-agent") in mng_ctx.config.agent_types
    assert mng_ctx.config.agent_types[AgentTypeName("custom-agent")].cli_args == ("--custom",)


# =============================================================================
# Tests for get_or_create_profile_dir
# =============================================================================


def test_get_or_create_profile_dir_creates_new_profile_when_no_config(tmp_path: Path) -> None:
    """get_or_create_profile_dir should create a new profile when config.toml doesn't exist."""
    base_dir = tmp_path / "mng"

    result = get_or_create_profile_dir(base_dir)

    # Should have created the directories
    assert (base_dir / "profiles").exists()
    assert result.parent == base_dir / "profiles"
    assert result.exists()

    # Should have written config.toml with the profile ID
    config_path = base_dir / "config.toml"
    assert config_path.exists()
    content = config_path.read_text()
    profile_id = result.name
    assert f'profile = "{profile_id}"' in content


def test_get_or_create_profile_dir_reads_existing_profile_from_config(tmp_path: Path) -> None:
    """get_or_create_profile_dir should read existing profile from config.toml."""
    base_dir = tmp_path / "mng"
    base_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir = base_dir / "profiles"
    profiles_dir.mkdir(exist_ok=True)

    # Create existing profile
    existing_profile_id = "existing123"
    existing_profile_dir = profiles_dir / existing_profile_id
    existing_profile_dir.mkdir(exist_ok=True)

    # Write config.toml pointing to existing profile
    config_path = base_dir / "config.toml"
    config_path.write_text(f'profile = "{existing_profile_id}"\n')

    result = get_or_create_profile_dir(base_dir)

    assert result == existing_profile_dir
    assert result.name == existing_profile_id


def test_get_or_create_profile_dir_creates_profile_dir_if_specified_but_missing(tmp_path: Path) -> None:
    """get_or_create_profile_dir should create profile dir if config.toml specifies it but dir doesn't exist."""
    base_dir = tmp_path / "mng"
    base_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir = base_dir / "profiles"
    profiles_dir.mkdir(exist_ok=True)

    # Write config.toml pointing to non-existent profile
    specified_profile_id = "specified456"
    config_path = base_dir / "config.toml"
    config_path.write_text(f'profile = "{specified_profile_id}"\n')

    result = get_or_create_profile_dir(base_dir)

    # Should have created the specified profile directory
    assert result == profiles_dir / specified_profile_id
    assert result.exists()


def test_get_or_create_profile_dir_handles_invalid_config_toml(tmp_path: Path) -> None:
    """get_or_create_profile_dir should handle invalid config.toml by creating new profile."""
    base_dir = tmp_path / "mng"
    base_dir.mkdir(parents=True, exist_ok=True)

    # Write invalid TOML
    config_path = base_dir / "config.toml"
    config_path.write_text("[invalid toml syntax")

    result = get_or_create_profile_dir(base_dir)

    # Should have created a new profile (with new config)
    assert result.exists()
    assert result.parent == base_dir / "profiles"

    # config.toml should have been overwritten with valid content
    new_content = config_path.read_text()
    assert 'profile = "' in new_content


def test_get_or_create_profile_dir_handles_config_without_profile_key(tmp_path: Path) -> None:
    """get_or_create_profile_dir should create new profile if config.toml has no 'profile' key."""
    base_dir = tmp_path / "mng"
    base_dir.mkdir(parents=True, exist_ok=True)

    # Write valid TOML but without profile key
    config_path = base_dir / "config.toml"
    config_path.write_text('other_key = "value"\n')

    result = get_or_create_profile_dir(base_dir)

    # Should have created a new profile
    assert result.exists()
    assert result.parent == base_dir / "profiles"


def test_get_or_create_profile_dir_returns_same_profile_on_subsequent_calls(tmp_path: Path) -> None:
    """get_or_create_profile_dir should return the same profile on subsequent calls."""
    base_dir = tmp_path / "mng"

    result1 = get_or_create_profile_dir(base_dir)
    result2 = get_or_create_profile_dir(base_dir)

    assert result1 == result2


# =============================================================================
# Tests for _get_or_create_user_id
# =============================================================================


def test_get_or_create_user_id_creates_new_id_when_file_missing(tmp_path: Path) -> None:
    """_get_or_create_user_id should create a new user ID when file doesn't exist."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    result = get_or_create_user_id(profile_dir)

    # Should return a non-empty string (hex UUID, which is 32 chars)
    assert result
    assert len(result) == 32

    # Should have written the ID to file
    user_id_file = profile_dir / "user_id"
    assert user_id_file.exists()
    assert user_id_file.read_text() == result


def test_get_or_create_user_id_reads_existing_id(tmp_path: Path) -> None:
    """_get_or_create_user_id should read existing user ID from file."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Create existing user_id file
    existing_id = "abcdef1234567890abcdef1234567890"
    user_id_file = profile_dir / "user_id"
    user_id_file.write_text(existing_id)

    result = get_or_create_user_id(profile_dir)

    assert result == existing_id


def test_get_or_create_user_id_strips_whitespace(tmp_path: Path) -> None:
    """_get_or_create_user_id should strip whitespace from existing ID."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Create existing user_id file with whitespace
    existing_id = "abcdef1234567890abcdef1234567890"
    user_id_file = profile_dir / "user_id"
    user_id_file.write_text(f"  {existing_id}  \n")

    result = get_or_create_user_id(profile_dir)

    assert result == existing_id


def test_get_or_create_user_id_returns_same_id_on_subsequent_calls(tmp_path: Path) -> None:
    """_get_or_create_user_id should return the same ID on subsequent calls."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    result1 = get_or_create_user_id(profile_dir)
    result2 = get_or_create_user_id(profile_dir)

    assert result1 == result2


# =============================================================================
# Tests for MNG_ALLOW_UNKNOWN_CONFIG via load_config
# =============================================================================


def test_load_config_rejects_unknown_fields_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cg: ConcurrencyGroup
) -> None:
    """load_config should raise on unknown config fields when MNG_ALLOW_UNKNOWN_CONFIG is not set."""
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MNG_PREFIX", raising=False)
    monkeypatch.delenv("MNG_HOST_DIR", raising=False)
    monkeypatch.delenv("MNG_ROOT_NAME", raising=False)
    monkeypatch.delenv("MNG_ALLOW_UNKNOWN_CONFIG", raising=False)

    mng_dir = tmp_path / ".mng"
    mng_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = get_or_create_profile_dir(mng_dir)
    settings_path = profile_dir / "settings.toml"
    settings_path.write_text('future_field = "hello"\n')

    with pytest.raises(ConfigParseError, match="Unknown configuration fields.*future_field"):
        load_config(pm=pm, context_dir=tmp_path, concurrency_group=cg)


def test_load_config_allows_unknown_fields_with_env_var(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cg: ConcurrencyGroup,
    log_warnings: list[str],
) -> None:
    """load_config should warn (not raise) on unknown fields when MNG_ALLOW_UNKNOWN_CONFIG is set."""
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MNG_ALLOW_UNKNOWN_CONFIG", "1")
    monkeypatch.delenv("MNG_PREFIX", raising=False)
    monkeypatch.delenv("MNG_HOST_DIR", raising=False)
    monkeypatch.delenv("MNG_ROOT_NAME", raising=False)

    mng_dir = tmp_path / ".mng"
    mng_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = get_or_create_profile_dir(mng_dir)
    settings_path = profile_dir / "settings.toml"
    settings_path.write_text('future_field = "hello"\n')

    mng_ctx = load_config(pm=pm, context_dir=tmp_path, concurrency_group=cg)
    assert mng_ctx.config.prefix == "mng-"
    assert any("future_field" in msg for msg in log_warnings)


# =============================================================================
# Tests for default_destroyed_host_persisted_seconds via load_config
# =============================================================================


def test_load_config_preserves_default_destroyed_host_persisted_seconds_from_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cg: ConcurrencyGroup
) -> None:
    """load_config should forward default_destroyed_host_persisted_seconds from TOML to the final config."""
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MNG_PREFIX", raising=False)
    monkeypatch.delenv("MNG_HOST_DIR", raising=False)
    monkeypatch.delenv("MNG_ROOT_NAME", raising=False)

    # Write a user config with custom default_destroyed_host_persisted_seconds
    mng_dir = tmp_path / ".mng"
    mng_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = get_or_create_profile_dir(mng_dir)
    settings_path = profile_dir / "settings.toml"
    settings_path.write_text("default_destroyed_host_persisted_seconds = 86400.0\n")

    mng_ctx = load_config(
        pm=pm,
        concurrency_group=cg,
        context_dir=tmp_path,
    )

    assert mng_ctx.config.default_destroyed_host_persisted_seconds == 86400.0


# =============================================================================
# Tests for _parse_commands with default_subcommand
# =============================================================================


def test_parse_commands_extracts_default_subcommand() -> None:
    """_parse_commands should extract default_subcommand from raw defaults."""
    raw = {"mng": {"default_subcommand": "list", "connect": False}}
    result = _parse_commands(raw)
    assert result["mng"].default_subcommand == "list"
    # default_subcommand should NOT appear in the defaults dict
    assert "default_subcommand" not in result["mng"].defaults
    assert result["mng"].defaults["connect"] is False


def test_parse_commands_handles_missing_default_subcommand() -> None:
    """_parse_commands should set default_subcommand to None when absent."""
    raw = {"create": {"new_host": "docker"}}
    result = _parse_commands(raw)
    assert result["create"].default_subcommand is None
    assert result["create"].defaults["new_host"] == "docker"


def test_parse_commands_empty_string_default_subcommand() -> None:
    """_parse_commands should preserve empty string default_subcommand."""
    raw = {"mng": {"default_subcommand": ""}}
    result = _parse_commands(raw)
    assert result["mng"].default_subcommand == ""


# =============================================================================
# Tests for block_disabled_plugins
# =============================================================================


def test_block_disabled_plugins_blocks_names_in_plugin_manager() -> None:
    """block_disabled_plugins should call pm.set_blocked for each disabled name."""
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)

    block_disabled_plugins(pm, frozenset({"modal", "docker"}))

    assert pm.is_blocked("modal")
    assert pm.is_blocked("docker")
    assert not pm.is_blocked("local")


def test_block_disabled_plugins_is_idempotent() -> None:
    """block_disabled_plugins should be safe to call multiple times."""
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)

    block_disabled_plugins(pm, frozenset({"modal"}))
    block_disabled_plugins(pm, frozenset({"modal"}))

    assert pm.is_blocked("modal")


# =============================================================================
# Tests for _normalize_cli_args_for_construct
# =============================================================================


def test_normalize_cli_args_no_cli_args_key() -> None:
    """_normalize_cli_args_for_construct should return the input unchanged when no cli_args key."""
    raw = {"some_key": "value"}
    result = _normalize_cli_args_for_construct(raw)
    assert result == {"some_key": "value"}


def test_normalize_cli_args_string_value() -> None:
    """_normalize_cli_args_for_construct should split a non-empty string into a tuple."""
    raw = {"cli_args": "--verbose --model opus"}
    result = _normalize_cli_args_for_construct(raw)
    assert result["cli_args"] == ("--verbose", "--model", "opus")


def test_normalize_cli_args_empty_string() -> None:
    """_normalize_cli_args_for_construct should convert an empty string to an empty tuple."""
    raw = {"cli_args": ""}
    result = _normalize_cli_args_for_construct(raw)
    assert result["cli_args"] == ()


def test_normalize_cli_args_list_value() -> None:
    """_normalize_cli_args_for_construct should convert a list to a tuple."""
    raw = {"cli_args": ["--verbose", "--model", "opus"]}
    result = _normalize_cli_args_for_construct(raw)
    assert result["cli_args"] == ("--verbose", "--model", "opus")


def test_normalize_cli_args_tuple_value() -> None:
    """_normalize_cli_args_for_construct should pass through a tuple."""
    raw = {"cli_args": ("--verbose",)}
    result = _normalize_cli_args_for_construct(raw)
    assert result["cli_args"] == ("--verbose",)


def test_normalize_cli_args_other_type_passes_through() -> None:
    """_normalize_cli_args_for_construct should pass through unrecognized types."""
    raw = {"cli_args": 42}
    result = _normalize_cli_args_for_construct(raw)
    assert result["cli_args"] == 42


# =============================================================================
# Tests for _parse_command_env_vars edge cases
# =============================================================================


def test_parse_command_env_vars_empty_suffix_after_prefix() -> None:
    """_parse_command_env_vars should skip when env key is exactly the prefix with nothing after."""
    environ = {"MNG_COMMANDS_": "value"}
    result = _parse_command_env_vars(environ)
    assert result == {}


def test_parse_command_env_vars_empty_command_name() -> None:
    """_parse_command_env_vars should skip when command name is empty (leading underscore)."""
    environ = {"MNG_COMMANDS__PARAM": "value"}
    result = _parse_command_env_vars(environ)
    assert result == {}


# =============================================================================
# Tests for load_config pytest guard
# =============================================================================


def test_load_config_raises_when_in_pytest_and_not_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cg: ConcurrencyGroup
) -> None:
    """load_config should raise ConfigParseError when is_allowed_in_pytest is False and PYTEST_CURRENT_TEST is set."""
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MNG_PREFIX", raising=False)
    monkeypatch.delenv("MNG_HOST_DIR", raising=False)
    monkeypatch.delenv("MNG_ROOT_NAME", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_something")

    # Write config that disables pytest
    mng_dir = tmp_path / ".mng"
    mng_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = get_or_create_profile_dir(mng_dir)
    settings_path = profile_dir / "settings.toml"
    settings_path.write_text("is_allowed_in_pytest = false\n")

    with pytest.raises(ConfigParseError, match="Running mng within pytest is not allowed"):
        load_config(pm=pm, concurrency_group=cg, context_dir=tmp_path)


# =============================================================================
# Tests for load_config with env command overrides
# =============================================================================


def test_load_config_applies_env_command_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cg: ConcurrencyGroup
) -> None:
    """load_config should merge env command overrides into the final config."""
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MNG_PREFIX", raising=False)
    monkeypatch.delenv("MNG_HOST_DIR", raising=False)
    monkeypatch.delenv("MNG_ROOT_NAME", raising=False)
    monkeypatch.setenv("MNG_COMMANDS_CREATE_CONNECT", "false")

    mng_ctx = load_config(pm=pm, concurrency_group=cg, context_dir=tmp_path)

    assert "create" in mng_ctx.config.commands
    assert mng_ctx.config.commands["create"].defaults.get("connect") == "false"
