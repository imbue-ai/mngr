from imbue.mngr.primitives import AgentTypeName

# =============================================================================
# Agent Plugin Registry
#
# Records which plugin registered each agent type name (including alias names).
# Keyed by the agent-type name string, valued by the pluggy plugin name the
# type was registered under (e.g. the setuptools entry-point name like
# "antigravity" or "pi_coding", or a built-in name like "command").
#
# This decouples the disabled-plugin check from the historical assumption that
# the agent-type name equals the plugin name -- which never held for plugins
# whose entry-point name differs from their registered type name (e.g. the
# "pi_coding" entry point registers the "pi-coding" type) or for aliases (e.g.
# the "antigravity" plugin also registering the "agy" alias).
# =============================================================================

_agent_plugin_registry: dict[AgentTypeName, str] = {}


def register_agent_plugin(
    agent_type: str,
    plugin_name: str,
) -> None:
    """Record the pluggy plugin name that registered an agent type."""
    _agent_plugin_registry[AgentTypeName(agent_type)] = plugin_name


def get_agent_plugin_name(agent_type: str) -> str | None:
    """Return the plugin name that registered an agent type, or None if unknown."""
    return _agent_plugin_registry.get(AgentTypeName(agent_type))


def reset_agent_plugin_registry() -> None:
    """Reset the registry. Used for test isolation."""
    _agent_plugin_registry.clear()
