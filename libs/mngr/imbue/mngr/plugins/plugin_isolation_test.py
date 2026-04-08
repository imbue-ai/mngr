"""Tests that verify plugin isolation works correctly under different configurations.

These tests exercise the ``enabled_plugins`` fixture override pattern to ensure
the plugin manager correctly blocks and enables plugins based on the configuration.
"""

import pluggy

from imbue.mngr.agents.agent_registry import list_registered_agent_types
from imbue.mngr.plugin_catalog import PLUGIN_CATALOG
from imbue.mngr.plugin_catalog import get_independent_entry_point_names
from imbue.mngr.primitives import PluginTier

# =============================================================================
# Default configuration (BASIC tier)
# =============================================================================


def test_default_config_loads_basic_tier_agent_types(plugin_manager: pluggy.PluginManager) -> None:
    """With default config, BASIC-tier agent types should be registered."""
    registered = list_registered_agent_types()
    assert "claude" in registered
    assert "opencode" in registered


def test_default_config_blocks_extra_tier_plugins(plugin_manager: pluggy.PluginManager) -> None:
    """With default config, EXTRA-tier plugins should be blocked."""
    extra_names = {e.entry_point_name for e in PLUGIN_CATALOG if e.tier == PluginTier.DEPENDENT}
    for name in extra_names:
        assert plugin_manager.is_blocked(name), f"EXTRA plugin {name} should be blocked by default"


# =============================================================================
# Helper validation
# =============================================================================


def test_get_independent_entry_point_names_matches_catalog() -> None:
    """get_independent_entry_point_names should return exactly the BASIC-tier entries."""
    basic_names = get_independent_entry_point_names()
    expected = {e.entry_point_name for e in PLUGIN_CATALOG if e.tier == PluginTier.INDEPENDENT}
    assert basic_names == expected
