from imbue.mngr.primitives import AgentTypeName

# =============================================================================
# Agent Alias Registry
#
# An alias is an alternate name for a canonical agent type. Aliases form a
# name-resolution layer that sits ABOVE the agent class/config registries:
# an alias is never itself a registered agent type, it merely resolves to one.
# Callers normalize an incoming name through this layer (via
# ``normalize_agent_type_name``) before consulting the type registries, so the
# distinction between a true agent type (a key in the class/config registries)
# and an alias (a key here) stays clean and non-overlapping.
# =============================================================================

_agent_alias_registry: dict[AgentTypeName, AgentTypeName] = {}


def register_agent_alias(
    alias: str,
    canonical_agent_type: str,
) -> None:
    """Record that an agent-type name is an alias of a canonical agent type."""
    _agent_alias_registry[AgentTypeName(alias)] = AgentTypeName(canonical_agent_type)


def unregister_agent_alias(alias: str) -> None:
    """Drop an alias from the registry, if present.

    Used when a user-defined custom agent type shadows a plugin-registered
    alias of the same name: the user's concrete type takes precedence (just as
    a registered type beats an alias at plugin-load time), so the alias is
    removed and the name resolves to the custom type instead.
    """
    _agent_alias_registry.pop(AgentTypeName(alias), None)


def is_agent_alias(agent_type: str) -> bool:
    """Whether an agent-type name is a registered alias rather than a canonical type."""
    return AgentTypeName(agent_type) in _agent_alias_registry


def normalize_agent_type_name(agent_type: str) -> str:
    """Resolve an alias to its canonical agent-type name, or return the name unchanged.

    Aliases never chain (an alias always points directly at a canonical type),
    so a single lookup is sufficient.
    """
    canonical = _agent_alias_registry.get(AgentTypeName(agent_type))
    return str(canonical) if canonical is not None else agent_type


def list_agent_aliases() -> dict[str, str]:
    """Return a copy of the alias-to-canonical-type mapping."""
    return {str(alias): str(canonical) for alias, canonical in _agent_alias_registry.items()}


def reset_agent_alias_registry() -> None:
    """Reset the registry. Used for test isolation."""
    _agent_alias_registry.clear()
