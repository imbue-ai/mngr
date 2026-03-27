"""Integration tests for external plugin loading via setuptools entry points.

These tests verify that external plugins (like mng_opencode) are properly
discovered and registered when installed in the same environment.
"""

from imbue.mng.agents.agent_registry import list_registered_agent_types
from imbue.mng.config.agent_config_registry import get_agent_config_class
from imbue.mng_opencode.plugin import OpenCodeAgentConfig


def test_external_plugin_agent_type_is_registered_via_entry_points() -> None:
    """Verify that external plugin agent types are discovered via entry points.

    The mng_opencode package registers the 'opencode' agent type via a
    setuptools entry point. This test verifies that the plugin is discovered
    and the agent type is available after loading.
    """
    # The plugin_manager fixture (autouse) already loads entry points,
    # so the opencode agent type should be registered
    registered_types = list_registered_agent_types()
    assert "opencode" in registered_types


def test_external_plugin_config_class_is_registered() -> None:
    """Verify that the external plugin's config class is used for its agent type."""
    config_class = get_agent_config_class("opencode")
    assert config_class is OpenCodeAgentConfig
