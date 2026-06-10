from imbue.mngr.primitives import AgentTypeName

# =============================================================================
# Agent Type Owner Registry
#
# Records which plugin registered each agent type -- its "owner". Keyed by the
# agent-type name string, valued by the pluggy plugin name the type was
# registered under (e.g. the setuptools entry-point name like "antigravity" or
# "pi_coding", or a built-in name like "command").
#
# This decouples the disabled-plugin check from the historical assumption that
# the agent-type name equals the plugin name -- which never held for plugins
# whose entry-point name differs from their registered type name (e.g. the
# "pi_coding" entry point registers the "pi-coding" type).
# =============================================================================

_owning_plugin_by_agent_type: dict[AgentTypeName, str] = {}


def register_agent_type_owner(
    agent_type: str,
    plugin_name: str,
) -> None:
    """Record the pluggy plugin name that registered (owns) an agent type."""
    _owning_plugin_by_agent_type[AgentTypeName(agent_type)] = plugin_name


def get_agent_type_owner(agent_type: str) -> str | None:
    """Return the plugin name that registered (owns) an agent type, or None if unknown."""
    return _owning_plugin_by_agent_type.get(AgentTypeName(agent_type))


def reset_agent_type_owner_registry() -> None:
    """Reset the registry. Used for test isolation."""
    _owning_plugin_by_agent_type.clear()
