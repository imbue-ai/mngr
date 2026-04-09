import pluggy

from imbue.claude_web_chat.hookspecs import ClaudeWebChatHookSpec

_plugin_manager: pluggy.PluginManager | None = None


def get_plugin_manager() -> pluggy.PluginManager:
    global _plugin_manager
    if _plugin_manager is None:
        _plugin_manager = pluggy.PluginManager("claude_web_chat")
        _plugin_manager.add_hookspecs(ClaudeWebChatHookSpec)
    return _plugin_manager
