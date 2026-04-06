"""Tests for agent_config_registry and agent_class_registry modules."""

import pytest
from pydantic import Field

from imbue.mngr.config.agent_class_registry import get_agent_class
from imbue.mngr.config.agent_class_registry import register_agent_class
from imbue.mngr.config.agent_class_registry import reset_agent_class_registry
from imbue.mngr.config.agent_class_registry import set_default_agent_class
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


class _SubclassAgentConfig(AgentTypeConfig):
    """Test subclass with an extra field for testing subclass-specific field handling."""

    extra_bool: bool = Field(default=False)
    extra_str: str | None = Field(default=None)


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


def test_apply_custom_overrides_applies_subclass_fields() -> None:
    """Subclass-specific fields set in the custom config should be applied to the parent."""
    parent = _SubclassAgentConfig()
    custom = _SubclassAgentConfig.model_construct(
        extra_bool=True,
        extra_str="hello",
        parent_type=AgentTypeName("test-parent"),
    )

    result = _apply_custom_overrides_to_parent_config(parent, custom)

    assert isinstance(result, _SubclassAgentConfig)
    assert result.extra_bool is True
    assert result.extra_str == "hello"


def test_apply_custom_overrides_preserves_unset_subclass_fields() -> None:
    """Subclass-specific fields NOT set in the custom config should keep parent defaults."""
    parent = _SubclassAgentConfig(extra_bool=True, extra_str="original")
    # Only set command, not the subclass fields
    custom = _SubclassAgentConfig.model_construct(
        command=CommandString("new-cmd"),
        parent_type=AgentTypeName("test-parent"),
    )

    result = _apply_custom_overrides_to_parent_config(parent, custom)

    assert isinstance(result, _SubclassAgentConfig)
    assert result.command == CommandString("new-cmd")
    assert result.extra_bool is True
    assert result.extra_str == "original"


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


def test_resolve_agent_type_inherits_parent_user_config() -> None:
    """resolve_agent_type should inherit settings from the parent type's user config.

    When the parent type itself has user-configured overrides (e.g. [agent_types.parent-type]),
    those should be inherited by child types, not just the bare defaults.
    """
    reset_agent_class_registry()
    reset_agent_config_registry()
    try:
        register_agent_class("parent-type", _FakeAgentClass)
        register_agent_config("parent-type", _SubclassAgentConfig)

        # Parent type has user-configured extra_bool=True
        parent_user_config = _SubclassAgentConfig.model_construct(
            extra_bool=True,
        )
        # Child type sets extra_str but not extra_bool
        child_config = _SubclassAgentConfig.model_construct(
            parent_type=AgentTypeName("parent-type"),
            extra_str="child-value",
        )

        config = MngrConfig(
            agent_types={
                AgentTypeName("parent-type"): parent_user_config,
                AgentTypeName("child-type"): child_config,
            },
        )

        result = resolve_agent_type(AgentTypeName("child-type"), config)

        assert result.agent_class is _FakeAgentClass
        resolved_config = result.agent_config
        assert isinstance(resolved_config, _SubclassAgentConfig)
        # extra_bool should be inherited from the parent's user config
        assert resolved_config.extra_bool is True
        # extra_str should come from the child's own config
        assert resolved_config.extra_str == "child-value"
    finally:
        reset_agent_class_registry()
        reset_agent_config_registry()


def test_resolve_agent_type_child_overrides_parent_user_config() -> None:
    """Child type fields should override inherited parent user config fields."""
    reset_agent_class_registry()
    reset_agent_config_registry()
    try:
        register_agent_class("parent-type", _FakeAgentClass)
        register_agent_config("parent-type", _SubclassAgentConfig)

        parent_user_config = _SubclassAgentConfig.model_construct(
            extra_bool=True,
            extra_str="parent-value",
        )
        child_config = _SubclassAgentConfig.model_construct(
            parent_type=AgentTypeName("parent-type"),
            extra_bool=False,
        )

        config = MngrConfig(
            agent_types={
                AgentTypeName("parent-type"): parent_user_config,
                AgentTypeName("child-type"): child_config,
            },
        )

        result = resolve_agent_type(AgentTypeName("child-type"), config)

        resolved_config = result.agent_config
        assert isinstance(resolved_config, _SubclassAgentConfig)
        # Child explicitly set extra_bool=False, should override parent's True
        assert resolved_config.extra_bool is False
        # extra_str not set in child, should inherit from parent
        assert resolved_config.extra_str == "parent-value"
    finally:
        reset_agent_class_registry()
        reset_agent_config_registry()


def test_resolve_agent_type_preserves_subclass_fields() -> None:
    """resolve_agent_type should preserve subclass-specific fields from custom config."""
    reset_agent_class_registry()
    reset_agent_config_registry()
    try:
        register_agent_class("test-parent", _FakeAgentClass)
        register_agent_config("test-parent", _SubclassAgentConfig)

        # Simulate what _parse_agent_types now produces: a subclass config
        # constructed with model_construct (so model_fields_set is accurate)
        custom_config = _SubclassAgentConfig.model_construct(
            parent_type=AgentTypeName("test-parent"),
            command=CommandString("custom-cmd"),
            cli_args=("--custom-arg",),
            extra_bool=True,
            extra_str="custom-value",
        )

        config = MngrConfig(
            agent_types={AgentTypeName("my-worker"): custom_config},
        )

        result = resolve_agent_type(AgentTypeName("my-worker"), config)

        assert result.agent_class is _FakeAgentClass
        assert isinstance(result.agent_config, _SubclassAgentConfig)
        assert result.agent_config.command == CommandString("custom-cmd")
        assert result.agent_config.cli_args == ("--custom-arg",)
        resolved_config = result.agent_config
        assert isinstance(resolved_config, _SubclassAgentConfig)
        assert resolved_config.extra_bool is True
        assert resolved_config.extra_str == "custom-value"
    finally:
        reset_agent_class_registry()
        reset_agent_config_registry()


# =============================================================================
# resolve_agent_type disabled plugin tests
# =============================================================================


def test_resolve_agent_type_raises_when_plugin_disabled() -> None:
    """resolve_agent_type should raise MngrError when the agent type's plugin is disabled."""
    reset_agent_class_registry()
    reset_agent_config_registry()
    try:
        set_default_agent_class(_FakeAgentClass)
        register_agent_class("my-plugin", _FakeAgentClass)
        register_agent_config("my-plugin", AgentTypeConfig)

        config = MngrConfig(disabled_plugins=frozenset({"my-plugin"}))

        with pytest.raises(MngrError, match="plugin 'my-plugin' is disabled"):
            resolve_agent_type(AgentTypeName("my-plugin"), config)
    finally:
        reset_agent_class_registry()
        reset_agent_config_registry()


def test_resolve_agent_type_raises_when_parent_type_plugin_disabled() -> None:
    """resolve_agent_type should raise when a custom type's parent_type plugin is disabled."""
    reset_agent_class_registry()
    reset_agent_config_registry()
    try:
        register_agent_class("parent-plugin", _FakeAgentClass)
        register_agent_config("parent-plugin", AgentTypeConfig)

        config = MngrConfig(
            agent_types={
                AgentTypeName("child-type"): AgentTypeConfig(
                    parent_type=AgentTypeName("parent-plugin"),
                ),
            },
            disabled_plugins=frozenset({"parent-plugin"}),
        )

        with pytest.raises(MngrError, match="plugin 'parent-plugin' is disabled"):
            resolve_agent_type(AgentTypeName("child-type"), config)
    finally:
        reset_agent_class_registry()
        reset_agent_config_registry()


def test_resolve_agent_type_raises_when_grandparent_plugin_disabled() -> None:
    """resolve_agent_type should walk the full parent chain and catch a disabled grandparent."""
    reset_agent_class_registry()
    reset_agent_config_registry()
    try:
        register_agent_class("root-plugin", _FakeAgentClass)
        register_agent_config("root-plugin", AgentTypeConfig)

        config = MngrConfig(
            agent_types={
                AgentTypeName("mid-type"): AgentTypeConfig(
                    parent_type=AgentTypeName("root-plugin"),
                ),
                AgentTypeName("leaf-type"): AgentTypeConfig(
                    parent_type=AgentTypeName("mid-type"),
                ),
            },
            disabled_plugins=frozenset({"root-plugin"}),
        )

        with pytest.raises(MngrError, match="plugin 'root-plugin' is disabled"):
            resolve_agent_type(AgentTypeName("leaf-type"), config)
    finally:
        reset_agent_class_registry()
        reset_agent_config_registry()


def test_resolve_agent_type_uses_explicit_plugin_field() -> None:
    """resolve_agent_type should use the explicit plugin field when set."""
    reset_agent_class_registry()
    reset_agent_config_registry()
    try:
        set_default_agent_class(_FakeAgentClass)

        config = MngrConfig(
            agent_types={
                AgentTypeName("my-type"): AgentTypeConfig(plugin="real-plugin"),
            },
            disabled_plugins=frozenset({"real-plugin"}),
        )

        with pytest.raises(MngrError, match="plugin 'real-plugin' is disabled"):
            resolve_agent_type(AgentTypeName("my-type"), config)
    finally:
        reset_agent_class_registry()
        reset_agent_config_registry()


def test_resolve_agent_type_explicit_plugin_field_overrides_name() -> None:
    """An explicit plugin field pointing to an enabled plugin should allow resolution even if the type name matches a disabled plugin."""
    reset_agent_class_registry()
    reset_agent_config_registry()
    try:
        set_default_agent_class(_FakeAgentClass)

        config = MngrConfig(
            agent_types={
                AgentTypeName("disabled-name"): AgentTypeConfig(plugin="enabled-plugin"),
            },
            disabled_plugins=frozenset({"disabled-name"}),
        )

        result = resolve_agent_type(AgentTypeName("disabled-name"), config)
        assert result.agent_class is _FakeAgentClass
    finally:
        reset_agent_class_registry()
        reset_agent_config_registry()


def test_resolve_agent_type_allows_non_disabled_plugin() -> None:
    """resolve_agent_type should work normally when the plugin is not disabled."""
    reset_agent_class_registry()
    reset_agent_config_registry()
    try:
        register_agent_class("enabled-plugin", _FakeAgentClass)
        register_agent_config("enabled-plugin", AgentTypeConfig)

        config = MngrConfig(disabled_plugins=frozenset({"other-plugin"}))

        result = resolve_agent_type(AgentTypeName("enabled-plugin"), config)
        assert result.agent_class is _FakeAgentClass
    finally:
        reset_agent_class_registry()
        reset_agent_config_registry()
