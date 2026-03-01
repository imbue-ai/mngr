from imbue.mng.errors import MngError
from imbue.mng.primitives import AgentTypeName

# =============================================================================
# Agent Class Registry
#
# Stores concrete agent class types (e.g. ClaudeAgent, BaseAgent).
# Uses bare `type` instead of `type[AgentInterface]` to avoid importing
# from the interfaces layer (which is above config in the hierarchy).
# =============================================================================

_agent_class_registry: dict[AgentTypeName, type] = {}
_default_agent_class: type | None = None


def register_agent_class(
    agent_type: str,
    agent_class: type,
) -> None:
    """Register a class for an agent type."""
    _agent_class_registry[AgentTypeName(agent_type)] = agent_class


def set_default_agent_class(agent_class: type) -> None:
    """Set the default agent class returned when a type is not registered."""
    global _default_agent_class
    _default_agent_class = agent_class


def get_agent_class(agent_type: str) -> type:
    """Get the agent class for an agent type.

    Returns the default agent class if no specific type is registered.
    Raises MngError if no default has been set.
    """
    key = AgentTypeName(agent_type)
    if key in _agent_class_registry:
        return _agent_class_registry[key]
    if _default_agent_class is not None:
        return _default_agent_class
    raise MngError(f"Unknown agent type '{agent_type}' and no default agent class set.")


def list_registered_agent_class_types() -> list[str]:
    """List all agent type names with registered classes."""
    return sorted(str(k) for k in _agent_class_registry.keys())


def reset_agent_class_registry() -> None:
    """Reset the registry. Used for test isolation."""
    global _default_agent_class
    _agent_class_registry.clear()
    _default_agent_class = None
