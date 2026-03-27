"""Tests for agent_config_registry and agent_class_registry modules."""

import pytest

from imbue.mngr.config.agent_class_registry import get_agent_class
from imbue.mngr.config.agent_class_registry import register_agent_class
from imbue.mngr.config.agent_class_registry import reset_agent_class_registry
from imbue.mngr.config.agent_config_registry import _apply_custom_overrides_to_parent_config
from imbue.mngr.config.agent_config_registry import get_agent_config_class
from imbue.mngr.config.agent_config_registry import list_registered_agent_config_types
from imbue.mngr.config.agent_config_registry import register_agent_config
from imbue.mngr.config.agent_config_registry import reset_agent_config_registry
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import Permission


def test_apply_custom_overrides_returns_parent_when_no_overrides() -> None:
    """_apply_custom_overrides_to_parent_config should return parent unchanged when custom has no overrides."""
    parent = AgentTypeConfig(cli_args=("--model", "opus"))
    custom = AgentTypeConfig()

    result = _apply_custom_overrides_to_parent_config(parent, custom)

    assert result is parent
    assert result.cli_args == ("--model", "opus")


def test_apply_custom_overrides_applies_command_override() -> None:
    """Custom config with a command should override the parent's command."""
    parent = AgentTypeConfig(command=CommandString("parent-cmd"))
    custom = AgentTypeConfig(command=CommandString("custom-cmd"))

    result = _apply_custom_overrides_to_parent_config(parent, custom)

    assert result is not parent
    assert result.command == CommandString("custom-cmd")


def test_apply_custom_overrides_applies_cli_args_override() -> None:
    """Custom config with cli_args should merge with parent's cli_args."""
    parent = AgentTypeConfig(cli_args=("--base",))
    custom = AgentTypeConfig(cli_args=("--extra",))

    result = _apply_custom_overrides_to_parent_config(parent, custom)

    assert result is not parent
    assert result.cli_args == ("--base", "--extra")


def test_apply_custom_overrides_applies_permissions_override() -> None:
    """Custom config with permissions should override the parent's permissions."""
    parent = AgentTypeConfig()
    custom = AgentTypeConfig(permissions=[Permission("network")])

    result = _apply_custom_overrides_to_parent_config(parent, custom)

    assert result is not parent
    assert result.permissions == [Permission("network")]


def test_apply_custom_overrides_applies_all_overrides_at_once() -> None:
    """All fields overridden at once should produce a merged config."""
    parent = AgentTypeConfig(cli_args=("--parent-arg",))
    custom = AgentTypeConfig(
        command=CommandString("my-cmd"),
        cli_args=("--custom-arg",),
        permissions=[Permission("disk")],
    )

    result = _apply_custom_overrides_to_parent_config(parent, custom)

    assert result.command == CommandString("my-cmd")
    assert result.cli_args == ("--parent-arg", "--custom-arg")
    assert result.permissions == [Permission("disk")]


# =============================================================================
# Registry function tests
# =============================================================================


def test_register_and_get_agent_config_class() -> None:
    """register_agent_config and get_agent_config_class should round-trip correctly."""
    reset_agent_config_registry()
    try:
        register_agent_config("test-type", AgentTypeConfig)
        result = get_agent_config_class("test-type")
        assert result is AgentTypeConfig
    finally:
        reset_agent_config_registry()


def test_get_agent_config_class_returns_base_for_unknown() -> None:
    """get_agent_config_class returns AgentTypeConfig for unregistered types."""
    reset_agent_config_registry()
    result = get_agent_config_class("nonexistent-type")
    assert result is AgentTypeConfig


def test_list_registered_agent_config_types() -> None:
    """list_registered_agent_config_types should return sorted registered type names."""
    reset_agent_config_registry()
    try:
        register_agent_config("zebra", AgentTypeConfig)
        register_agent_config("alpha", AgentTypeConfig)
        result = list_registered_agent_config_types()
        assert result == ["alpha", "zebra"]
    finally:
        reset_agent_config_registry()


def test_get_agent_class_raises_when_unknown_and_no_default() -> None:
    """get_agent_class should raise MngrError when agent type is unknown and no default is set."""
    reset_agent_class_registry()
    with pytest.raises(MngrError, match="Unknown agent type 'nonexistent'"):
        get_agent_class("nonexistent")


# =============================================================================
# resolve_agent_type tests
# =============================================================================


class _FakeAgentClass:
    """Fake agent class for testing resolve_agent_type."""

    pass


def test_resolve_agent_type_with_parent_type() -> None:
    """resolve_agent_type should resolve through parent_type for custom types."""
    reset_agent_class_registry()
    reset_agent_config_registry()
    try:
        register_agent_class("parent-type", _FakeAgentClass)
        register_agent_config("parent-type", AgentTypeConfig)

        config = MngrConfig(
            agent_types={
                AgentTypeName("child-type"): AgentTypeConfig(
                    parent_type=AgentTypeName("parent-type"),
                    cli_args=("--custom-arg",),
                ),
            },
        )

        result = resolve_agent_type(AgentTypeName("child-type"), config)

        assert result.agent_class is _FakeAgentClass
        assert result.agent_config.cli_args == ("--custom-arg",)
    finally:
        reset_agent_class_registry()
        reset_agent_config_registry()


def test_resolve_agent_type_without_parent_type() -> None:
    """resolve_agent_type should use the direct type registration when no parent_type."""
    reset_agent_class_registry()
    reset_agent_config_registry()
    try:
        register_agent_class("direct-type", _FakeAgentClass)
        register_agent_config("direct-type", AgentTypeConfig)

        config = MngrConfig()
        result = resolve_agent_type(AgentTypeName("direct-type"), config)

        assert result.agent_class is _FakeAgentClass
        assert isinstance(result.agent_config, AgentTypeConfig)
    finally:
        reset_agent_class_registry()
        reset_agent_config_registry()
