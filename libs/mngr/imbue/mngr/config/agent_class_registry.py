from imbue.mngr.errors import UnknownAgentTypeError
from imbue.mngr.primitives import AgentTypeName

# =============================================================================
# Agent Class Registry
#
# Stores concrete agent class types (e.g. ClaudeAgent, BaseAgent).
# Uses bare `type` instead of `type[AgentInterface]` to avoid importing
# from the interfaces layer (which is above config in the hierarchy).
# =============================================================================

_agent_class_registry: dict[AgentTypeName, type] = {}

# Used by the on-disk load path when an agent's recorded type is no longer
# registered (e.g. the plugin was uninstalled). Set by the agents layer at
# plugin-load time so the hosts layer can degrade gracefully without
# importing concretely from agents (which the import-linter forbids).
_orphan_agent_class: type | None = None


def register_agent_class(
    agent_type: str,
    agent_class: type,
) -> None:
    """Register a class for an agent type."""
    _agent_class_registry[AgentTypeName(agent_type)] = agent_class


def get_agent_class(agent_type: str) -> type:
    """Get the agent class registered directly for an agent type.

    This is a low-level primitive: it does a flat registry lookup and does NOT
    resolve aliases or config-defined subtypes (those with a ``parent_type``).
    Passing such a name raises UnknownAgentTypeError even though it is a valid
    type. Use ``resolve_agent_type`` whenever the input may be an alias or a
    subtype and you want the resolved class -- it walks the parent chain. Prefer
    it by default; reach for ``get_agent_class`` only when you specifically need
    the class registered for an exact, canonical type name.

    Raises UnknownAgentTypeError if no class is registered for this type.
    """
    key = AgentTypeName(agent_type)
    if key in _agent_class_registry:
        return _agent_class_registry[key]
    raise UnknownAgentTypeError(agent_type)


def is_agent_class_registered(agent_type: str) -> bool:
    """Check if an agent class is registered for the given type."""
    return AgentTypeName(agent_type) in _agent_class_registry


def list_registered_agent_class_types() -> list[str]:
    """List all agent type names with registered classes."""
    return sorted(str(k) for k in _agent_class_registry.keys())


def set_orphan_agent_class(agent_class: type) -> None:
    """Set the class to use when loading an agent whose type is no longer registered.

    Consulted only by the on-disk load path in the hosts layer, so commands
    like ``mngr destroy`` keep working after a plugin is uninstalled. New
    agents created via ``resolve_agent_type`` are unaffected -- they still
    require a registered type or a valid ``parent_type`` chain.
    """
    global _orphan_agent_class
    _orphan_agent_class = agent_class


def get_orphan_agent_class() -> type | None:
    """Return the orphan fallback class set by ``set_orphan_agent_class``, or None."""
    return _orphan_agent_class


def reset_agent_class_registry() -> None:
    """Reset the registry. Used for test isolation."""
    global _orphan_agent_class
    _agent_class_registry.clear()
    _orphan_agent_class = None
