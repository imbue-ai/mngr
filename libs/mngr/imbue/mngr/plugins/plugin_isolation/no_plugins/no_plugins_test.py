"""Tests with all plugins disabled -- simulates a minimal mngr install."""

import pluggy

from imbue.mngr.agents.agent_registry import list_registered_agent_types
from imbue.mngr.plugin_catalog import PLUGIN_CATALOG


def test_no_external_agent_types_registered(plugin_manager: pluggy.PluginManager) -> None:
    """With no plugins, no external agent types should be registered."""
    registered = list_registered_agent_types()
    assert "claude" not in registered
    assert "opencode" not in registered


def test_all_cataloged_plugins_are_blocked(plugin_manager: pluggy.PluginManager) -> None:
    """Every cataloged plugin should be blocked."""
    for entry in PLUGIN_CATALOG:
        assert plugin_manager.is_blocked(entry.entry_point_name), f"Plugin {entry.entry_point_name} should be blocked"
