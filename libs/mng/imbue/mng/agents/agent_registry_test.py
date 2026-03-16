"""Tests for agent registry."""

from pydantic import Field

from imbue.mng.agents.agent_registry import _register_agent
from imbue.mng.agents.agent_registry import list_registered_agent_types
from imbue.mng.agents.base_agent import BaseAgent
from imbue.mng.agents.default_plugins.codex_agent import CodexAgentConfig
from imbue.mng.config.agent_class_registry import get_agent_class
from imbue.mng.config.agent_config_registry import get_agent_config_class
from imbue.mng.config.agent_config_registry import register_agent_config
from imbue.mng.config.agent_config_registry import resolve_agent_type
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import MngConfig
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString


def test_get_agent_config_class_returns_base_for_unregistered_type() -> None:
    """Unknown agent types should return the base AgentTypeConfig class."""
    config_class = get_agent_config_class("unknown-agent-type")
    assert config_class == AgentTypeConfig


def test_get_agent_config_class_returns_registered_type() -> None:
    """Registered agent types should return their specific config class."""
    config_class = get_agent_config_class("codex")
    assert config_class == CodexAgentConfig


def test_list_registered_agent_types_includes_builtin_types() -> None:
    """Built-in agent types should be in the registry."""
    agent_types = list_registered_agent_types()
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


def test_get_agent_class_returns_base_agent_for_unknown_type() -> None:
    """Unknown agent type should return the default BaseAgent class."""
    agent_class = get_agent_class("unknown-type")
    assert agent_class == BaseAgent


def test_resolve_agent_type_returns_base_agent_for_unknown_type() -> None:
    """Resolving an unknown type should return BaseAgent with base config."""
    config = MngConfig()
    resolved = resolve_agent_type(AgentTypeName("unknown-command"), config)

    assert resolved.agent_class == BaseAgent
    assert type(resolved.agent_config) is AgentTypeConfig


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


def test_register_agent_registers_class_and_config() -> None:
    """_register_agent should register both class and config."""
    _register_agent(
        agent_type="runtime-test-type",
        agent_class=BaseAgent,
        config_class=AgentTypeConfig,
    )

    assert get_agent_class("runtime-test-type") == BaseAgent
    assert get_agent_config_class("runtime-test-type") == AgentTypeConfig
