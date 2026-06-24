"""Tests for agent registry."""

import pluggy
import pytest
from pydantic import Field

from imbue.mngr import hookimpl
from imbue.mngr.agents.agent_registry import _register_agent
from imbue.mngr.agents.agent_registry import list_available_agent_types
from imbue.mngr.agents.agent_registry import list_registered_agent_types
from imbue.mngr.agents.agent_registry import list_selectable_agent_type_names
from imbue.mngr.agents.agent_registry import load_agents_from_plugins
from imbue.mngr.agents.agent_registry import reset_agent_registry
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.default_plugins.command_agent import CommandAgent
from imbue.mngr.agents.default_plugins.headless_command_agent import HeadlessCommandConfig
from imbue.mngr.config.agent_alias_registry import is_agent_alias
from imbue.mngr.config.agent_alias_registry import normalize_agent_type_name
from imbue.mngr.config.agent_class_registry import get_agent_class
from imbue.mngr.config.agent_class_registry import is_agent_class_registered
from imbue.mngr.config.agent_config_registry import get_agent_config_class
from imbue.mngr.config.agent_config_registry import is_known_agent_type
from imbue.mngr.config.agent_config_registry import register_agent_config
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.overlay_merge import merge_models_via_overlay
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UnknownAgentTypeError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.plugins import hookspecs
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString


def test_get_agent_config_class_returns_base_for_unregistered_type() -> None:
    """Unknown agent types should return the base AgentTypeConfig class."""
    config_class = get_agent_config_class("unknown-agent-type")
    assert config_class == AgentTypeConfig


def test_get_agent_config_class_returns_registered_type() -> None:
    """Registered agent types should return their specific config class."""
    config_class = get_agent_config_class("headless_command")
    assert config_class == HeadlessCommandConfig


def test_list_registered_agent_types_includes_builtin_types() -> None:
    """Built-in agent types should be in the registry."""
    agent_types = list_registered_agent_types()
    assert "command" in agent_types


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

    merged, _ = merge_models_via_overlay(base, override)

    assert merged.command == CommandString("override-command")


def test_agent_type_config_merge_keeps_base_command_when_override_none() -> None:
    """Merge should keep base command when override is None."""
    base = AgentTypeConfig(command=CommandString("base-command"))
    override = AgentTypeConfig()

    merged, _ = merge_models_via_overlay(base, override)

    assert merged.command == CommandString("base-command")


def test_agent_type_config_merge_replaces_cli_args() -> None:
    """Merge assigns cli_args from override (no concat under assign-by-default)."""
    base = AgentTypeConfig(cli_args=("--verbose",))
    override = AgentTypeConfig(cli_args=("--debug",))

    merged, _ = merge_models_via_overlay(base, override)

    assert merged.cli_args == ("--debug",)


def test_agent_type_config_merge_cli_args_with_empty_base() -> None:
    """Merge should use override cli_args when base is empty."""
    base = AgentTypeConfig()
    override = AgentTypeConfig(cli_args=("--debug",))

    merged, _ = merge_models_via_overlay(base, override)

    assert merged.cli_args == ("--debug",)


def test_agent_type_config_merge_cli_args_with_empty_override() -> None:
    """Merge should keep base cli_args when override is empty."""
    base = AgentTypeConfig(cli_args=("--verbose",))
    override = AgentTypeConfig()

    merged, _ = merge_models_via_overlay(base, override)

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
            AgentTypeName("my-custom"): AgentTypeConfig(parent_type=AgentTypeName("command")),
        },
    )

    available = list_available_agent_types(config)

    # The command agent type is registered directly in core, so it must always appear.
    assert "command" in available
    # And the user-config-defined name must appear too.
    assert "my-custom" in available
    # Output is sorted for stable display.
    assert available == sorted(available)


class _FakeAgent(BaseAgent):
    """A stand-in agent class for alias registration tests."""


class _FakeAgentConfig(AgentTypeConfig):
    """A stand-in agent config class for alias registration tests."""


class _AliasPlugin:
    """A fake plugin that registers a type plus an alias for it."""

    @hookimpl
    def register_agent_type(self) -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
        return ("fake-agent", _FakeAgent, _FakeAgentConfig)

    @hookimpl
    def register_agent_aliases(self) -> dict[str, str]:
        return {"fa": "fake-agent"}


class _BadAliasPlugin:
    """A fake plugin whose alias collides with an existing type and whose target is missing."""

    @hookimpl
    def register_agent_type(self) -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
        return ("other-agent", _FakeAgent, _FakeAgentConfig)

    @hookimpl
    def register_agent_aliases(self) -> dict[str, str]:
        # "command" collides with a built-in type; "missing" points at an unregistered target.
        return {"command": "other-agent", "no-alias": "missing"}


def _load_with_plugins(*plugins: object) -> pluggy.PluginManager:
    reset_agent_registry()
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    for idx, plugin in enumerate(plugins):
        pm.register(plugin, name=f"fake_plugin_{idx}")
    load_agents_from_plugins(pm)
    return pm


def test_alias_resolves_to_canonical_class_and_config() -> None:
    """Resolving an alias yields the canonical type's class and config."""
    _load_with_plugins(_AliasPlugin())

    resolved = resolve_agent_type(AgentTypeName("fa"), MngrConfig())
    assert resolved.agent_class is _FakeAgent
    assert isinstance(resolved.agent_config, _FakeAgentConfig)


def test_alias_is_not_itself_a_registered_agent_type() -> None:
    """An alias lives in the resolution layer, not the class/config registries."""
    _load_with_plugins(_AliasPlugin())

    # The alias name is not a distinct registered type ...
    assert not is_agent_class_registered("fa")
    assert "fa" not in list_registered_agent_types()
    # ... but it is recognized as a known type name and as an alias.
    assert is_agent_alias("fa")
    assert is_known_agent_type("fa", MngrConfig())
    assert normalize_agent_type_name("fa") == "fake-agent"


def test_alias_excluded_from_available_types_but_included_in_selectable() -> None:
    """Aliases are hidden from the distinct-type list but offered for completion."""
    _load_with_plugins(_AliasPlugin())

    config = MngrConfig()
    assert "fa" not in list_available_agent_types(config)
    assert "fake-agent" in list_available_agent_types(config)
    assert "fa" in list_selectable_agent_type_names(config)


@pytest.mark.allow_warnings
def test_alias_is_skipped_when_name_collides_with_existing_type() -> None:
    """An alias whose name is already a registered type must not shadow that type."""
    _load_with_plugins(_BadAliasPlugin())

    # The built-in "command" type's class must be untouched by the colliding alias.
    assert get_agent_class("command") is CommandAgent
    assert not is_agent_alias("command")


@pytest.mark.allow_warnings
def test_alias_is_skipped_when_target_is_not_registered() -> None:
    """An alias pointing at an unregistered target is dropped rather than registered."""
    _load_with_plugins(_BadAliasPlugin())

    assert not is_agent_alias("no-alias")


def test_disabled_plugin_check_attributes_alias_to_owning_plugin() -> None:
    """Resolving an alias whose owning plugin is disabled names that plugin, not the alias."""
    _load_with_plugins(_AliasPlugin())

    config = MngrConfig(disabled_plugins=frozenset({"fake_plugin_0"}))
    with pytest.raises(MngrError, match="plugin 'fake_plugin_0' is disabled"):
        resolve_agent_type(AgentTypeName("fa"), config)


def test_disabled_plugin_check_uses_registered_plugin_for_mismatched_type_name() -> None:
    """When a type name differs from its plugin name, the disabled check names the plugin.

    This mirrors the real ``pi_coding`` plugin registering the ``pi-coding``
    type: disabling the plugin (by its registration name) must report that
    name in the error, not the type name.
    """

    class _MismatchedPlugin:
        @hookimpl
        def register_agent_type(self) -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
            return ("type-name", _FakeAgent, _FakeAgentConfig)

    reset_agent_registry()
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    pm.register(_MismatchedPlugin(), name="plugin-name")
    load_agents_from_plugins(pm)

    config = MngrConfig(disabled_plugins=frozenset({"plugin-name"}))
    with pytest.raises(MngrError, match="plugin 'plugin-name' is disabled"):
        resolve_agent_type(AgentTypeName("type-name"), config)
