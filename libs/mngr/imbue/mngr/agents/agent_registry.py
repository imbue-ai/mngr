from __future__ import annotations

import pluggy

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.default_plugins import codex_agent
from imbue.mngr.agents.default_plugins import command_agent
from imbue.mngr.agents.default_plugins import headless_command_agent
from imbue.mngr.config.agent_class_registry import list_registered_agent_class_types
from imbue.mngr.config.agent_class_registry import register_agent_class
from imbue.mngr.config.agent_class_registry import reset_agent_class_registry
from imbue.mngr.config.agent_class_registry import set_orphan_agent_class
from imbue.mngr.config.agent_config_registry import list_registered_agent_config_types
from imbue.mngr.config.agent_config_registry import register_agent_config
from imbue.mngr.config.agent_config_registry import reset_agent_config_registry
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.interfaces.agent import AgentInterface

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

    # Wire BaseAgent as the orphan-load fallback so the hosts layer can
    # degrade gracefully for on-disk agents whose plugin was uninstalled,
    # without importing concretely from the agents layer (forbidden by the
    # import-linter contract).
    set_orphan_agent_class(BaseAgent)

    # Register built-in agent type classes (each has a hookimpl static method)
    # Claude-based agent types are registered via entry points from the mngr_claude plugin
    pm.register(codex_agent, name="codex")
    pm.register(command_agent, name="command")
    pm.register(headless_command_agent, name="headless_command")

    # Call the hook to get all agent type registrations. pluggy's hook caller
    # already drops any hookimpl that returns None, and the hook is typed to
    # return a (name, class, config) tuple, so every registration is a 3-tuple.
    # Unpacking directly means a plugin that violates that contract fails loudly
    # here instead of being silently skipped.
    all_registrations = pm.hook.register_agent_type()

    for agent_type_name, agent_class, config_class in all_registrations:
        _register_agent_internal(agent_type_name, agent_class, config_class)

    _registry_state["agents_loaded"] = True


def _register_agent_internal(
    agent_type: str,
    agent_class: type[AgentInterface] | None = None,
    config_class: type[AgentTypeConfig] | None = None,
) -> None:
    """Internal function to register an agent type."""
    # Registering neither a class nor a config is a silent no-op that almost
    # certainly means a caller forgot to pass one of them; explode on it rather
    # than pretending the registration succeeded.
    assert agent_class is not None or config_class is not None, (
        f"Cannot register agent type {agent_type!r} with neither an agent class nor a config class; "
        "at least one must be provided."
    )
    if agent_class is not None:
        register_agent_class(agent_type, agent_class)
    if config_class is not None:
        register_agent_config(agent_type, config_class)


def list_registered_agent_types() -> list[str]:
    """List all registered agent type names (from both class and config registries)."""
    class_types = set(list_registered_agent_class_types())
    config_types = set(list_registered_agent_config_types())
    return sorted(class_types | config_types)


def list_available_agent_types(config: MngrConfig) -> list[str]:
    """List every agent type the user can pick.

    This is the union of plugin-registered types (``list_registered_agent_types``)
    and any user-config-defined types under ``[agent_types.X]`` (subclass
    types that delegate to a registered ``parent_type``). This is the
    canonical list to show the user in pickers, error messages, and tab
    completions -- the same set the completion cache exposes for ``--type``.
    """
    custom = [str(k) for k in config.agent_types.keys()]
    return sorted(set(list_registered_agent_types() + custom))


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
