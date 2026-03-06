"""Tests for agent registry."""

from typing import Any

import pytest
from pydantic import Field

from imbue.mng.agents.agent_registry import list_registered_agent_types
from imbue.mng.agents.base_agent import BaseAgent
from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgent
from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgentConfig
from imbue.mng.agents.default_plugins.codex_agent import CodexAgentConfig
from imbue.mng.config.agent_class_registry import get_agent_class
from imbue.mng.config.agent_config_registry import ResolvedAgentType
from imbue.mng.config.agent_config_registry import get_agent_config_class
from imbue.mng.config.agent_config_registry import register_agent_config
from imbue.mng.config.agent_config_registry import resolve_agent_type
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import MngConfig
from imbue.mng.errors import ConfigParseError
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import Permission


def test_get_agent_config_class_returns_base_for_unregistered_type() -> None:
    """Unknown agent types should return the base AgentTypeConfig class."""
    config_class = get_agent_config_class("unknown-agent-type")
    assert config_class == AgentTypeConfig


def test_get_agent_config_class_returns_registered_type() -> None:
    """Registered agent types should return their specific config class."""
    config_class = get_agent_config_class("claude")
    assert config_class == ClaudeAgentConfig

    config_class = get_agent_config_class("codex")
    assert config_class == CodexAgentConfig


def test_list_registered_agent_types_includes_builtin_types() -> None:
    """Built-in agent types should be in the registry."""
    agent_types = list_registered_agent_types()
    assert "claude" in agent_types
    assert "codex" in agent_types


def test_codex_agent_config_has_default_command() -> None:
    """Codex agent config should have a default command."""
    config = CodexAgentConfig()
    assert config.command == CommandString("codex")


def test_register_custom_agent_type() -> None:
    """Should be able to register custom agent types."""

    class CustomAgentConfig(AgentTypeConfig):
        """Test custom agent config."""

        command: CommandString = Field(
            default=CommandString("custom-agent"),
            description="Custom agent command",
        )

    register_agent_config("test-custom", CustomAgentConfig)

    config_class = get_agent_config_class("test-custom")
    assert config_class == CustomAgentConfig

    config = config_class()
    assert config.command == CommandString("custom-agent")


def test_agent_type_config_merge_preserves_command() -> None:
    """Base AgentTypeConfig merge should handle command field."""
    base = AgentTypeConfig(command=CommandString("base-command"))
    override = AgentTypeConfig(command=CommandString("override-command"))

    merged = base.merge_with(override)

    assert merged.command == CommandString("override-command")


def test_agent_type_config_merge_keeps_base_command_when_override_none() -> None:
    """Merge should keep base command when override is None."""
    base = AgentTypeConfig(command=CommandString("base-command"))
    override = AgentTypeConfig()

    merged = base.merge_with(override)

    assert merged.command == CommandString("base-command")


def test_agent_type_config_merge_concatenates_cli_args() -> None:
    """Merge should concatenate cli_args from base and override."""
    base = AgentTypeConfig(cli_args=("--verbose",))
    override = AgentTypeConfig(cli_args=("--debug",))

    merged = base.merge_with(override)

    assert merged.cli_args == ("--verbose", "--debug")


def test_agent_type_config_merge_cli_args_with_empty_base() -> None:
    """Merge should use override cli_args when base is empty."""
    base = AgentTypeConfig()
    override = AgentTypeConfig(cli_args=("--debug",))

    merged = base.merge_with(override)

    assert merged.cli_args == ("--debug",)


def test_agent_type_config_merge_cli_args_with_empty_override() -> None:
    """Merge should keep base cli_args when override is empty."""
    base = AgentTypeConfig(cli_args=("--verbose",))
    override = AgentTypeConfig()

    merged = base.merge_with(override)

    assert merged.cli_args == ("--verbose",)


def test_get_agent_class_returns_claude_agent_for_claude_type() -> None:
    """Claude agent type should return ClaudeAgent class."""
    agent_class = get_agent_class("claude")
    assert agent_class == ClaudeAgent


def test_claude_agent_config_merge_with_wrong_type_raises_error() -> None:
    """ClaudeAgentConfig.merge_with should raise ConfigParseError for wrong type."""
    base = ClaudeAgentConfig()
    override = CodexAgentConfig()

    with pytest.raises(ConfigParseError, match="Cannot merge ClaudeAgentConfig"):
        base.merge_with(override)


def test_resolve_agent_type_returns_claude_for_registered_type() -> None:
    """Resolving a registered type should return its class and default config."""
    config = MngConfig()
    resolved = resolve_agent_type(AgentTypeName("claude"), config)

    assert resolved.agent_class == ClaudeAgent
    assert isinstance(resolved.agent_config, ClaudeAgentConfig)
    assert resolved.agent_config.command == CommandString("claude")


def test_resolve_agent_type_returns_base_agent_for_unknown_type() -> None:
    """Resolving an unknown type should return BaseAgent with base config."""
    config = MngConfig()
    resolved = resolve_agent_type(AgentTypeName("unknown-command"), config)

    assert resolved.agent_class == BaseAgent
    assert type(resolved.agent_config) is AgentTypeConfig


def _resolve_custom_claude_type(**config_overrides: Any) -> ResolvedAgentType:
    """Helper to resolve a custom type with parent_type=claude and given overrides."""
    custom_config = AgentTypeConfig(
        parent_type=AgentTypeName("claude"),
        **config_overrides,
    )
    config = MngConfig(
        agent_types={AgentTypeName("my_claude"): custom_config},
    )
    return resolve_agent_type(AgentTypeName("my_claude"), config)


def test_resolve_agent_type_with_custom_type_uses_parent_class() -> None:
    """A custom type with parent_type should use the parent's agent class."""
    resolved = _resolve_custom_claude_type(cli_args=("--model", "opus"))

    assert resolved.agent_class == ClaudeAgent
    assert isinstance(resolved.agent_config, ClaudeAgentConfig)


def test_resolve_agent_type_with_custom_type_merges_cli_args() -> None:
    """A custom type should merge its cli_args onto the parent config."""
    resolved = _resolve_custom_claude_type(cli_args=("--model", "opus"))

    assert resolved.agent_config.cli_args == ("--model", "opus")


def test_resolve_agent_type_with_custom_type_overrides_command() -> None:
    """A custom type with a command override should apply it to the parent config."""
    resolved = _resolve_custom_claude_type(command=CommandString("my-custom-claude-wrapper"))

    assert resolved.agent_config.command == CommandString("my-custom-claude-wrapper")


def test_resolve_agent_type_with_custom_type_preserves_parent_specific_fields() -> None:
    """Custom type config should retain parent-specific fields like sync_home_settings."""
    resolved = _resolve_custom_claude_type(cli_args=("--model", "opus"))

    # ClaudeAgentConfig has sync_home_settings=True by default
    assert isinstance(resolved.agent_config, ClaudeAgentConfig)
    assert resolved.agent_config.sync_home_settings is True


def test_resolve_agent_type_with_custom_type_overrides_permissions() -> None:
    """Custom type permissions should override (replace) parent permissions."""
    resolved = _resolve_custom_claude_type(
        permissions=[Permission("github"), Permission("docker")],
    )

    assert resolved.agent_config.permissions == [Permission("github"), Permission("docker")]


def test_resolve_agent_type_with_custom_type_empty_permissions_keeps_parent() -> None:
    """Custom type with no permissions should keep the parent's default permissions."""
    resolved = _resolve_custom_claude_type(cli_args=("--model", "opus"))

    assert resolved.agent_config.permissions == []


def test_resolve_agent_type_with_override_for_registered_type() -> None:
    """A config override for a registered type (no parent_type) uses registered class."""
    custom_config = AgentTypeConfig(
        cli_args=("--extra-flag",),
    )
    config = MngConfig(
        agent_types={AgentTypeName("claude"): custom_config},
    )

    resolved = resolve_agent_type(AgentTypeName("claude"), config)

    assert resolved.agent_class == ClaudeAgent
    # Since there's no parent_type, the custom_config is used directly
    assert resolved.agent_config.cli_args == ("--extra-flag",)


def test_resolve_agent_type_custom_type_without_parent_uses_base_agent() -> None:
    """A custom type without parent_type should use BaseAgent."""
    custom_config = AgentTypeConfig(
        command=CommandString("my-agent-binary"),
    )
    config = MngConfig(
        agent_types={AgentTypeName("my_custom"): custom_config},
    )

    resolved = resolve_agent_type(AgentTypeName("my_custom"), config)

    assert resolved.agent_class == BaseAgent
    assert resolved.agent_config.command == CommandString("my-agent-binary")
