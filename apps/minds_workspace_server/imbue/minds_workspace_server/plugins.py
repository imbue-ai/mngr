import pluggy

from imbue.minds_workspace_server.hookspecs import MindsWorkspaceServerHookSpec

_plugin_manager: pluggy.PluginManager | None = None


def get_plugin_manager() -> pluggy.PluginManager:
    global _plugin_manager
    if _plugin_manager is None:
        _plugin_manager = pluggy.PluginManager("minds_workspace_server")
        _plugin_manager.add_hookspecs(MindsWorkspaceServerHookSpec)
    return _plugin_manager
