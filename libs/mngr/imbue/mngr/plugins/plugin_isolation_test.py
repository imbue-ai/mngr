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
# Default configuration (INDEPENDENT tier)
# =============================================================================


def test_default_config_loads_independent_tier_agent_types(plugin_manager: pluggy.PluginManager) -> None:
    """With default config, INDEPENDENT-tier agent types should be registered."""
    registered = list_registered_agent_types()
    assert "claude" in registered
    assert "opencode" in registered


def test_default_config_blocks_dependent_tier_plugins(plugin_manager: pluggy.PluginManager) -> None:
    """With default config, DEPENDENT-tier plugins should be blocked."""
    dependent_names = {e.entry_point_name for e in PLUGIN_CATALOG if e.tier == PluginTier.DEPENDENT}
    # Guard against vacuous success: if the catalog ever has no DEPENDENT-tier
    # entries, the loop below would assert nothing. Fail loudly instead.
    assert dependent_names, "Expected at least one DEPENDENT-tier plugin in the catalog"
    for name in dependent_names:
        assert plugin_manager.is_blocked(name), f"DEPENDENT plugin {name} should be blocked by default"


# =============================================================================
# Helper validation
# =============================================================================


def test_get_independent_entry_point_names_matches_catalog() -> None:
    """get_independent_entry_point_names should return exactly the INDEPENDENT-tier entries."""
    independent_names = get_independent_entry_point_names()
    expected = {e.entry_point_name for e in PLUGIN_CATALOG if e.tier == PluginTier.INDEPENDENT}
    # Guard against both sides being empty (which would make the equality vacuous).
    assert independent_names, "Expected at least one INDEPENDENT-tier plugin in the catalog"
    assert independent_names == expected
