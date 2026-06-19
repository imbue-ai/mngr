"""Tests for claude agent type registration in the agent registry."""

from typing import Any

from imbue.mngr.agents.agent_registry import list_registered_agent_types
from imbue.mngr.config.agent_class_registry import get_agent_class
from imbue.mngr.config.agent_config_registry import ResolvedAgentType
from imbue.mngr.config.agent_config_registry import get_agent_config_class
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
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


def test_resolve_agent_type_returns_claude_for_registered_type() -> None:
    """Resolving a registered type should return its class and default config."""
    config = MngrConfig()
    resolved = resolve_agent_type(AgentTypeName("claude"), config)

    assert resolved.agent_class == ClaudeAgent
    assert isinstance(resolved.agent_config, ClaudeAgentConfig)
    assert resolved.agent_config.command == CommandString("claude")


def _resolve_custom_claude_type(
    parent_config: ClaudeAgentConfig | None = None,
    **config_overrides: Any,
) -> ResolvedAgentType:
    """Helper to resolve a custom type with parent_type=claude and given overrides.

    If parent_config is provided, it is registered as the user config for the
    parent "claude" type, so its non-default fields participate in the merge.
    """
    custom_config = AgentTypeConfig(
        parent_type=AgentTypeName("claude"),
        **config_overrides,
    )
    agent_types: dict[AgentTypeName, AgentTypeConfig] = {AgentTypeName("my_claude"): custom_config}
    if parent_config is not None:
        agent_types[AgentTypeName("claude")] = parent_config
    config = MngrConfig(agent_types=agent_types)
    return resolve_agent_type(AgentTypeName("my_claude"), config)


def test_resolve_agent_type_with_custom_type_uses_parent_class_and_merges_cli_args() -> None:
    """A custom type with parent_type should use the parent's agent class and merge its cli_args."""
    resolved = _resolve_custom_claude_type(cli_args=("--model", "opus"))

    assert resolved.agent_class == ClaudeAgent
    assert isinstance(resolved.agent_config, ClaudeAgentConfig)
    assert resolved.agent_config.cli_args == ("--model", "opus")


def test_resolve_agent_type_with_custom_type_overrides_command() -> None:
    """A custom type with a command override should apply it to the parent config."""
    resolved = _resolve_custom_claude_type(command=CommandString("my-custom-claude-wrapper"))

    assert resolved.agent_config.command == CommandString("my-custom-claude-wrapper")


def test_resolve_agent_type_with_custom_type_preserves_parent_specific_fields() -> None:
    """A non-default parent-specific field (sync_home_settings) should survive the merge.

    sync_home_settings defaults to True, so the parent config explicitly sets it to
    False; if the merge dropped the field it would re-default to True and this would fail.
    """
    resolved = _resolve_custom_claude_type(
        parent_config=ClaudeAgentConfig(sync_home_settings=False),
        cli_args=("--model", "opus"),
    )

    assert isinstance(resolved.agent_config, ClaudeAgentConfig)
    assert resolved.agent_config.sync_home_settings is False


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
