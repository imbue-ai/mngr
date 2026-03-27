"""Tests for claude agent type registration in the agent registry."""

from typing import Any

import pytest

from imbue.mngr.agents.agent_registry import list_registered_agent_types
from imbue.mngr.agents.default_plugins.codex_agent import CodexAgentConfig
from imbue.mngr.config.agent_class_registry import get_agent_class
from imbue.mngr.config.agent_config_registry import ResolvedAgentType
from imbue.mngr.config.agent_config_registry import get_agent_config_class
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import Permission
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_claude.plugin import ClaudeAgentConfig


def test_get_agent_config_class_returns_claude_config() -> None:
    """Claude agent type should return ClaudeAgentConfig class."""
    config_class = get_agent_config_class("claude")
    assert config_class == ClaudeAgentConfig


def test_list_registered_agent_types_includes_claude() -> None:
    """Claude agent type should be in the registry."""
    agent_types = list_registered_agent_types()
    assert "claude" in agent_types


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
    config = MngrConfig()
    resolved = resolve_agent_type(AgentTypeName("claude"), config)

    assert resolved.agent_class == ClaudeAgent
    assert isinstance(resolved.agent_config, ClaudeAgentConfig)
    assert resolved.agent_config.command == CommandString("claude")


def _resolve_custom_claude_type(**config_overrides: Any) -> ResolvedAgentType:
    """Helper to resolve a custom type with parent_type=claude and given overrides."""
    custom_config = AgentTypeConfig(
        parent_type=AgentTypeName("claude"),
        **config_overrides,
    )
    config = MngrConfig(
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
    config = MngrConfig(
        agent_types={AgentTypeName("claude"): custom_config},
    )

    resolved = resolve_agent_type(AgentTypeName("claude"), config)

    assert resolved.agent_class == ClaudeAgent
    assert resolved.agent_config.cli_args == ("--extra-flag",)
