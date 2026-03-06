from __future__ import annotations

import pluggy

from imbue.mng.agents.base_agent import BaseAgent
from imbue.mng.agents.default_plugins import claude_agent
from imbue.mng.agents.default_plugins import code_guardian_agent
from imbue.mng.agents.default_plugins import codex_agent
from imbue.mng.agents.default_plugins import fixme_fairy_agent
from imbue.mng.config.agent_class_registry import list_registered_agent_class_types
from imbue.mng.config.agent_class_registry import register_agent_class
from imbue.mng.config.agent_class_registry import reset_agent_class_registry
from imbue.mng.config.agent_class_registry import set_default_agent_class
from imbue.mng.config.agent_config_registry import list_registered_agent_config_types
from imbue.mng.config.agent_config_registry import register_agent_config
from imbue.mng.config.agent_config_registry import reset_agent_config_registry
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.interfaces.agent import AgentInterface

# =============================================================================
# Agent Registry - plugin loading and convenience functions
# =============================================================================

# Use a mutable container to track state without 'global' keyword
_registry_state: dict[str, bool] = {"agents_loaded": False}


def reset_agent_registry() -> None:
    """Reset the agent registry to its initial state.

    This is primarily used for test isolation to ensure a clean state between tests.
    """
    reset_agent_class_registry()
    reset_agent_config_registry()
    _registry_state["agents_loaded"] = False


def load_agents_from_plugins(pm: pluggy.PluginManager) -> None:
    """Load agent types from plugins via the register_agent_type hook."""
    if _registry_state["agents_loaded"]:
        return

    # Set the default agent class (used when a type name is not registered)
    set_default_agent_class(BaseAgent)

    # Register built-in agent type classes (each has a hookimpl static method)
    pm.register(claude_agent, name="claude")
    pm.register(code_guardian_agent, name="code_guardian")
    pm.register(codex_agent, name="codex")
    pm.register(fixme_fairy_agent, name="fixme_fairy")

    # Call the hook to get all agent type registrations
    # Each implementation returns a single tuple
    all_registrations = pm.hook.register_agent_type()

    for registration in all_registrations:
        if registration is not None:
            agent_type_name, agent_class, config_class = registration
            _register_agent_internal(agent_type_name, agent_class, config_class)

    _registry_state["agents_loaded"] = True


def _register_agent_internal(
    agent_type: str,
    agent_class: type[AgentInterface] | None = None,
    config_class: type[AgentTypeConfig] | None = None,
) -> None:
    """Internal function to register an agent type."""
    if agent_class is not None:
        register_agent_class(agent_type, agent_class)
    if config_class is not None:
        register_agent_config(agent_type, config_class)


def list_registered_agent_types() -> list[str]:
    """List all registered agent type names (from both class and config registries)."""
    class_types = set(list_registered_agent_class_types())
    config_types = set(list_registered_agent_config_types())
    return sorted(class_types | config_types)


def _register_agent(
    agent_type: str,
    agent_class: type[AgentInterface] | None = None,
    config_class: type[AgentTypeConfig] | None = None,
) -> None:
    """Register agent class and/or config for an agent type at runtime.

    This is a convenience function for programmatic registration, useful for
    testing or dynamic agent type creation. For plugins, prefer using the
    @hookimpl decorator with register_agent_type().
    """
    _register_agent_internal(agent_type, agent_class, config_class)
