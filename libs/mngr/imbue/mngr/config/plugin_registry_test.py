"""Tests for plugin registry."""

from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr.config.plugin_registry import get_plugin_config_class
from imbue.mngr.config.plugin_registry import list_registered_plugins
from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr.config.plugin_registry import reset_plugin_config_registry


class CustomPluginConfig(PluginConfig):
    """Test custom plugin config."""

    custom_field: str = "default"


class FirstPluginConfig(PluginConfig):
    """First trivial plugin config for re-registration tests."""

    first_field: str = "first"


class SecondPluginConfig(PluginConfig):
    """Second trivial plugin config for re-registration tests."""

    second_field: str = "second"


def test_get_plugin_config_class_returns_base_for_unknown() -> None:
    """get_plugin_config_class should return PluginConfig for unknown plugin."""
    config_class = get_plugin_config_class("unknown-plugin-xyz")
    assert config_class is PluginConfig


def test_register_plugin_config_stores_custom_config() -> None:
    """register_plugin_config should store the custom config class."""
    register_plugin_config("test-plugin", CustomPluginConfig)
    config_class = get_plugin_config_class("test-plugin")
    assert config_class is CustomPluginConfig


def test_list_registered_plugins_returns_sorted_list() -> None:
    """list_registered_plugins should return the exact registered names, sorted.

    Reset first so the assertion sees only the names this test registers,
    independent of any import-time plugin registrations the autouse fixture
    preserves (it snapshots/restores rather than clearing).
    """
    reset_plugin_config_registry()
    register_plugin_config("zebra-plugin", PluginConfig)
    register_plugin_config("alpha-plugin", PluginConfig)

    assert list_registered_plugins() == ["alpha-plugin", "zebra-plugin"]


def test_register_plugin_config_latest_class_wins() -> None:
    """Re-registering the same plugin name with a different config class
    replaces the earlier registration; the latest class wins.
    """
    register_plugin_config("dup-plugin", FirstPluginConfig)
    register_plugin_config("dup-plugin", SecondPluginConfig)

    assert get_plugin_config_class("dup-plugin") is SecondPluginConfig


def test_list_registered_plugins_empty_on_fresh_registry() -> None:
    """A freshly-reset registry reports no registered plugins."""
    # The autouse fixture already resets the registry per-test; reset again
    # explicitly so this test is self-contained and unambiguous.
    reset_plugin_config_registry()

    assert list_registered_plugins() == []
