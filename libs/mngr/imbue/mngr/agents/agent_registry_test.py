"""Tests for agent registry."""

import pytest
from pydantic import Field

from imbue.mngr.agents.agent_registry import _register_agent
from imbue.mngr.agents.agent_registry import list_available_agent_types
from imbue.mngr.agents.agent_registry import list_registered_agent_types
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.default_plugins.codex_agent import CodexAgentConfig
from imbue.mngr.config.agent_class_registry import get_agent_class
from imbue.mngr.config.agent_config_registry import get_agent_config_class
from imbue.mngr.config.agent_config_registry import register_agent_config
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.errors import UnknownAgentTypeError
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString


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


def test_agent_type_config_merge_replaces_cli_args() -> None:
    """Merge assigns cli_args from override (no concat under assign-by-default)."""
    base = AgentTypeConfig(cli_args=("--verbose",))
    override = AgentTypeConfig(cli_args=("--debug",))

    merged = base.merge_with(override)

    assert merged.cli_args == ("--debug",)


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


def test_get_agent_class_raises_for_unknown_type() -> None:
    """Unknown agent type should raise UnknownAgentTypeError (no silent BaseAgent fallback)."""
    with pytest.raises(UnknownAgentTypeError, match="Unknown agent type 'unknown-type-xyz'"):
        get_agent_class("unknown-type-xyz")


def test_resolve_agent_type_raises_for_unknown_type() -> None:
    """Resolving an unknown type should raise UnknownAgentTypeError."""
    config = MngrConfig()
    with pytest.raises(UnknownAgentTypeError, match="Unknown agent type 'unknown-command-xyz'"):
        resolve_agent_type(AgentTypeName("unknown-command-xyz"), config)


def test_resolve_agent_type_custom_type_without_parent_raises() -> None:
    """A custom type whose name is not registered and has no parent_type should raise.

    The documented way to declare a TOML-only generic command is to set
    ``parent_type = "command"``; bare ``[agent_types.X]`` blocks without
    either a registered class or a parent_type are not a supported shape.
    """
    custom_config = AgentTypeConfig(
        command=CommandString("my-agent-binary"),
    )
    config = MngrConfig(
        agent_types={AgentTypeName("my_custom"): custom_config},
    )

    with pytest.raises(UnknownAgentTypeError, match="Unknown agent type 'my_custom'"):
        resolve_agent_type(AgentTypeName("my_custom"), config)


def test_register_agent_registers_class_and_config() -> None:
    """_register_agent should register both class and config."""
    _register_agent(
        agent_type="runtime-test-type",
        agent_class=BaseAgent,
        config_class=AgentTypeConfig,
    )

    assert get_agent_class("runtime-test-type") == BaseAgent
    assert get_agent_config_class("runtime-test-type") == AgentTypeConfig


def test_register_agent_with_neither_class_nor_config_raises() -> None:
    """Registering an agent type with neither a class nor a config is a no-op bug and must explode."""
    with pytest.raises(AssertionError, match="neither an agent class nor a config class"):
        _register_agent(agent_type="empty-registration-type")


def test_list_available_agent_types_unions_registered_and_user_config() -> None:
    """list_available_agent_types must include both plugin-registered and user-config-defined types.

    Users can define their own agent types under ``[agent_types.X]`` in
    settings.toml (subclass types that delegate to a registered
    parent_type). Those must show up in the same list the tab-completion
    cache and the `mngr plugin list --kind agent-type` filter use, so the
    user sees them in pickers and error messages.
    """
    config = MngrConfig(
        agent_types={
            AgentTypeName("my-custom"): AgentTypeConfig(parent_type=AgentTypeName("codex")),
        },
    )

    available = list_available_agent_types(config)

    # The codex agent type ships in-tree, so it must always appear.
    assert "codex" in available
    # And the user-config-defined name must appear too.
    assert "my-custom" in available
    # Output is sorted for stable display.
    assert available == sorted(available)
