"""Tests with only one plugin enabled -- verifies granular control."""

import pluggy

from imbue.mngr.agents.agent_registry import list_registered_agent_types


def test_only_claude_loaded(plugin_manager: pluggy.PluginManager) -> None:
    """Only claude should be registered, not other agent types."""
    registered = list_registered_agent_types()
    assert "claude" in registered
    assert "opencode" not in registered


# The two checks below intentionally re-assert the block/unblock decision the
# `plugin_manager` fixture made (it calls `set_blocked` for everything outside
# `enabled_plugins`). They are cheap tripwires on that wiring; the observable
# effect of the decision -- which agent types end up registered -- is covered by
# `test_only_claude_loaded` above.
def test_claude_is_not_blocked(plugin_manager: pluggy.PluginManager) -> None:
    assert not plugin_manager.is_blocked("claude")


def test_opencode_is_blocked(plugin_manager: pluggy.PluginManager) -> None:
    assert plugin_manager.is_blocked("opencode")
