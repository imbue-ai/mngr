from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr.primitives import PluginName

# =============================================================================
# Plugin Config Registry
# =============================================================================

_plugin_config_registry: dict[PluginName, type[PluginConfig]] = {}


def register_plugin_config(
    plugin_name: str,
    config_class: type[PluginConfig],
) -> None:
    """Register a plugin config class for a plugin."""
    _plugin_config_registry[PluginName(plugin_name)] = config_class


def get_plugin_config_class(plugin_name: str) -> type[PluginConfig]:
    """Get the config class for a plugin.

    Returns the base PluginConfig if no specific type is registered.
    """
    key = PluginName(plugin_name)
    if key not in _plugin_config_registry:
        return PluginConfig
    return _plugin_config_registry[key]


def list_registered_plugins() -> list[str]:
    """List all registered plugin names."""
    return sorted(str(k) for k in _plugin_config_registry.keys())
