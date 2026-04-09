"""Tests with all agent-type plugins enabled."""

import pluggy

from imbue.mngr.agents.agent_registry import list_registered_agent_types


def test_all_specified_agent_types_registered(plugin_manager: pluggy.PluginManager) -> None:
    """All agent-type entry points should be loaded."""
    registered = list_registered_agent_types()
    assert "claude" in registered
    assert "opencode" in registered


def test_non_agent_extras_still_blocked(plugin_manager: pluggy.PluginManager) -> None:
    """Plugins not in the enabled set should remain blocked."""
    assert plugin_manager.is_blocked("ttyd")
    assert plugin_manager.is_blocked("kanpan")
