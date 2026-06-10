from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pluggy
from loguru import logger

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.default_plugins import command_agent
from imbue.mngr.agents.default_plugins import headless_command_agent
from imbue.mngr.config.agent_alias_registry import is_agent_alias
from imbue.mngr.config.agent_alias_registry import list_agent_aliases
from imbue.mngr.config.agent_alias_registry import register_agent_alias
from imbue.mngr.config.agent_alias_registry import reset_agent_alias_registry
from imbue.mngr.config.agent_class_registry import is_agent_class_registered
from imbue.mngr.config.agent_class_registry import list_registered_agent_class_types
from imbue.mngr.config.agent_class_registry import register_agent_class
from imbue.mngr.config.agent_class_registry import reset_agent_class_registry
from imbue.mngr.config.agent_class_registry import set_orphan_agent_class
from imbue.mngr.config.agent_config_registry import is_agent_config_registered
from imbue.mngr.config.agent_config_registry import list_registered_agent_config_types
from imbue.mngr.config.agent_config_registry import register_agent_config
from imbue.mngr.config.agent_config_registry import reset_agent_config_registry
from imbue.mngr.config.agent_plugin_registry import register_agent_type_owner
from imbue.mngr.config.agent_plugin_registry import reset_agent_type_owner_registry
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
    reset_agent_type_owner_registry()
    reset_agent_alias_registry()
    _registry_state["agents_loaded"] = False


def load_agents_from_plugins(pm: pluggy.PluginManager) -> None:
    """Load agent types and aliases from plugins via the register hooks."""
    if _registry_state["agents_loaded"]:
        return

    # Wire BaseAgent as the orphan-load fallback so the hosts layer can
    # degrade gracefully for on-disk agents whose plugin was uninstalled,
    # without importing concretely from the agents layer (forbidden by the
    # import-linter contract).
    set_orphan_agent_class(BaseAgent)

    # Register built-in agent type classes (each has a hookimpl static method).
    # Agent-type plugins (claude, codex, antigravity, ...) are registered via
    # setuptools entry points from their own packages (e.g. imbue-mngr-codex).
    pm.register(command_agent, name="command")
    pm.register(headless_command_agent, name="headless_command")

    # Register agent types, recording which plugin produced each registration
    # so the disabled-plugin check (and aliases) can attribute a type to its
    # real owning plugin instead of assuming the type name equals the plugin
    # name. Each result is paired with its registering plugin's name.
    #
    # Iterate in reversed registration order: registration silently overwrites
    # on a duplicate type name (last write wins), and pluggy's flattened
    # ``hook()`` call -- which this loop replaces -- yields results in reversed
    # registration order. Reversing here preserves that "first-registered
    # plugin wins on a duplicate type name" precedence (and keeps the recorded
    # owner consistent with the winning class registration).
    for registration, plugin_name in reversed(_agent_type_registrations_by_plugin(pm)):
        if registration is not None:
            agent_type_name, agent_class, config_class = registration
            _register_agent_internal(agent_type_name, agent_class, config_class)
            register_agent_type_owner(agent_type_name, plugin_name)

    # Register aliases after all canonical types exist, so an alias can point
    # at any registered type. Aliases live in the alias-resolution layer (not
    # the type registries): callers normalize a name through them before any
    # registry lookup.
    for alias_mapping, plugin_name in _agent_alias_mappings_by_plugin(pm):
        if alias_mapping is not None:
            _register_aliases(alias_mapping, plugin_name)

    _registry_state["agents_loaded"] = True


# The declared result type of the register_agent_type hook (a hook may also
# return None, handled by callers).
_AgentTypeRegistration = tuple[str, type[AgentInterface] | None, type[AgentTypeConfig] | None]


def _agent_type_registrations_by_plugin(
    pm: pluggy.PluginManager,
) -> list[tuple[_AgentTypeRegistration | None, str]]:
    """Return each register_agent_type result paired with its plugin name.

    pluggy's flattened ``hook()`` call drops the originating plugin, so we go
    through the individual hook implementations to recover it. ``HookImpl``'s
    ``function`` is pluggy-typed as returning ``object``, so the cast here
    re-applies the hook's declared result type.
    """
    return [
        (cast("_AgentTypeRegistration | None", hookimpl.function()), hookimpl.plugin_name)
        for hookimpl in pm.hook.register_agent_type.get_hookimpls()
    ]


def _agent_alias_mappings_by_plugin(
    pm: pluggy.PluginManager,
) -> list[tuple[Mapping[str, str] | None, str]]:
    """Return each register_agent_aliases result paired with its plugin name.

    See ``_agent_type_registrations_by_plugin`` for why the cast is needed.
    """
    return [
        (cast("Mapping[str, str] | None", hookimpl.function()), hookimpl.plugin_name)
        for hookimpl in pm.hook.register_agent_aliases.get_hookimpls()
    ]


def _register_aliases(
    canonical_name_by_alias: Mapping[str, str],
    plugin_name: str,
) -> None:
    """Record aliases pointing at already-registered canonical agent types.

    Aliases are stored in the alias-resolution layer, not the type registries:
    a name that resolves through an alias is normalized to its canonical type
    before any registry lookup. Skips an alias whose canonical target is not a
    registered type, or whose name collides with an existing type or alias, so
    plugins cannot shadow types or each other.
    """
    for alias_name, canonical_name in canonical_name_by_alias.items():
        if not is_agent_class_registered(canonical_name):
            logger.warning(
                "Skipped alias '{}' because its target type '{}' (from plugin '{}') is not registered",
                alias_name,
                canonical_name,
                plugin_name,
            )
            continue
        if is_agent_class_registered(alias_name) or is_agent_config_registered(alias_name):
            logger.warning(
                "Skipped alias '{}' (from plugin '{}') because that name is already a registered agent type",
                alias_name,
                plugin_name,
            )
            continue
        if is_agent_alias(alias_name):
            logger.warning(
                "Skipped alias '{}' (from plugin '{}') because that name is already a registered alias",
                alias_name,
                plugin_name,
            )
            continue
        register_agent_alias(alias_name, canonical_name)


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


def list_available_agent_types(config: MngrConfig) -> list[str]:
    """List every canonical agent type the user can pick (excluding aliases).

    This is the union of plugin-registered types (``list_registered_agent_types``)
    and any user-config-defined types under ``[agent_types.X]`` (subclass
    types that delegate to a registered ``parent_type``). Aliases are
    deliberately excluded -- this is the list of distinct agent types, used
    for pickers, error messages, and ``mngr plugin list --kind agent-type``.
    For tab completion (where typing an alias should be offered), use
    ``list_selectable_agent_type_names`` instead.
    """
    custom = [str(k) for k in config.agent_types.keys()]
    return sorted(set(list_registered_agent_types() + custom))


def list_selectable_agent_type_names(config: MngrConfig) -> list[str]:
    """List every name a user may pass for an agent type, including aliases.

    This is ``list_available_agent_types`` plus all registered alias names. It
    is the set tab completion offers for ``--type`` / the positional type
    argument, since an alias is a valid thing to type even though it is not a
    distinct agent type.
    """
    return sorted(set(list_available_agent_types(config) + list(list_agent_aliases().keys())))


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
