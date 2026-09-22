"""Unit tests for the plugin registration in :mod:`imbue.mngr_latchkey.plugin`."""

import pluggy

from imbue.mngr_latchkey import plugin as latchkey_plugin


def test_latchkey_plugin_registers_its_host_created_hook_through_the_mngr_entry_point(
    plugin_manager: pluggy.PluginManager,
) -> None:
    """The gateway-URL fallback only helps if ``mngr create`` actually calls it, which the entry point decides."""
    registered_plugins = [hookimpl.plugin for hookimpl in plugin_manager.hook.on_host_created.get_hookimpls()]

    assert latchkey_plugin in registered_plugins
