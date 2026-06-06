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

    Returns the base PluginConfig if no specific type is registered. This
    fall-back-instead-of-raise behavior is intentional and differs from
    get_provider_config_class / get_agent_class (which raise on unknown names):
    a [plugins.<name>] block may legitimately reference a plugin whose package
    is not installed yet (e.g. when the disabled-plugins pre-reader or
    _apply_plugin_overrides constructs a config for a not-yet-installed plugin),
    and the base PluginConfig carries the universally-valid fields needed there.
    """
    key = PluginName(plugin_name)
    if key not in _plugin_config_registry:
        return PluginConfig
    return _plugin_config_registry[key]


def list_registered_plugins() -> list[str]:
    """List all registered plugin names."""
    return sorted(str(k) for k in _plugin_config_registry.keys())
