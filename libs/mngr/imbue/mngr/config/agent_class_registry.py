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


def register_agent_class(
    agent_type: str,
    agent_class: type,
) -> None:
    """Register a class for an agent type."""
    _agent_class_registry[AgentTypeName(agent_type)] = agent_class


def get_agent_class(agent_type: str) -> type:
    """Get the agent class for an agent type.

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


def reset_agent_class_registry() -> None:
    """Reset the registry. Used for test isolation."""
    _agent_class_registry.clear()
