"""Tests for plugin registry."""

from imbue.mng.config.data_types import PluginConfig
from imbue.mng.config.plugin_registry import get_plugin_config_class
from imbue.mng.config.plugin_registry import list_registered_plugins
from imbue.mng.config.plugin_registry import register_plugin_config


class CustomPluginConfig(PluginConfig):
    """Test custom plugin config."""

    custom_field: str = "default"


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
    """list_registered_plugins should return sorted list of registered plugins."""
    # Register some plugins to ensure the list is populated
    register_plugin_config("zebra-plugin", PluginConfig)
    register_plugin_config("alpha-plugin", PluginConfig)

    plugins = list_registered_plugins()
    assert isinstance(plugins, list)
    # Verify it's sorted
    assert plugins == sorted(plugins)
    # Verify our registered plugins are in the list
    assert "alpha-plugin" in plugins
    assert "zebra-plugin" in plugins
