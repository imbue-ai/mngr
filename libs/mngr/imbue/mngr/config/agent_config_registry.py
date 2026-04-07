from typing import Any

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.config.agent_class_registry import get_agent_class
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import merge_cli_args
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentTypeName

# Fields on AgentTypeConfig that are routing metadata (not runtime config values).
# These are skipped when applying custom overrides to a parent config.
_METADATA_FIELDS: frozenset[str] = frozenset({"parent_type", "plugin"})

# =============================================================================
# Agent Config Registry
# =============================================================================

_agent_config_registry: dict[AgentTypeName, type[AgentTypeConfig]] = {}


def register_agent_config(
    agent_type: str,
    config_class: type[AgentTypeConfig],
) -> None:
    """Register a config class for an agent type."""
    _agent_config_registry[AgentTypeName(agent_type)] = config_class


def get_agent_config_class(agent_type: str) -> type[AgentTypeConfig]:
    """Get the config class for an agent type.

    Returns the base AgentTypeConfig if no specific type is registered.
    """
    key = AgentTypeName(agent_type)
    if key not in _agent_config_registry:
        return AgentTypeConfig
    return _agent_config_registry[key]


def list_registered_agent_config_types() -> list[str]:
    """List all agent type names with registered config classes."""
    return sorted(str(k) for k in _agent_config_registry.keys())


def reset_agent_config_registry() -> None:
    """Reset the registry. Used for test isolation."""
    _agent_config_registry.clear()


# =============================================================================
# Agent Type Resolution
# =============================================================================


class ResolvedAgentType(FrozenModel):
    """Result of resolving an agent type, including parent type resolution for custom types."""

    model_config = {"arbitrary_types_allowed": True}

    agent_class: type = Field(description="The concrete AgentInterface subclass to use")
    agent_config: AgentTypeConfig = Field(description="The merged agent type config")


@pure
def _apply_custom_overrides_to_parent_config(
    parent_config: AgentTypeConfig,
    custom_config: AgentTypeConfig,
) -> AgentTypeConfig:
    """Apply custom type overrides onto a parent config instance.

    Handles the case where parent_config may be a subclass of AgentTypeConfig
    (e.g., ClaudeAgentConfig) by constructing a new instance of the parent's
    concrete class with the base fields overridden. Iterates over all fields
    that were explicitly set in the custom config (including subclass-specific
    fields like trust_working_directory).
    """
    explicitly_set_fields = custom_config.model_fields_set
    if not explicitly_set_fields - _METADATA_FIELDS:
        return parent_config

    custom_values = custom_config.model_dump()
    updates: list[tuple[str, Any]] = []

    for field_name in explicitly_set_fields:
        if field_name in _METADATA_FIELDS:
            continue
        elif field_name == "cli_args":
            # cli_args uses merge semantics (concatenation)
            merged_cli_args = merge_cli_args(parent_config.cli_args, custom_config.cli_args)
            if merged_cli_args != parent_config.cli_args:
                updates.append((field_name, merged_cli_args))
        else:
            # All other fields: override wins
            updates.append((field_name, custom_values[field_name]))

    if not updates:
        return parent_config

    return parent_config.model_copy_update(*updates)


def _check_agent_type_not_disabled(
    agent_type: AgentTypeName,
    config: MngrConfig,
) -> None:
    """Raise MngrError if the agent type or any ancestor in its parent chain is disabled.

    At each level, uses the explicit ``plugin`` field if set, otherwise
    falls back to ``parent_type`` (if set) or the type name -- mirroring
    how ``_parse_providers`` resolves the plugin for a provider block.

    Walks the chain: agent_type -> parent_type -> parent's parent_type -> ...
    until we hit a type with no parent_type or one that is not defined in
    config.agent_types.
    """
    current_cfg = config.agent_types.get(agent_type)
    checked: str | None = str(agent_type)
    seen: set[str] = set()
    while checked is not None and checked not in seen:
        seen.add(checked)
        # If this level has an explicit plugin field, use it and stop walking.
        if current_cfg is not None and current_cfg.plugin is not None:
            if current_cfg.plugin in config.disabled_plugins:
                raise MngrError(
                    f"Agent type '{agent_type}' cannot be used because plugin "
                    f"'{current_cfg.plugin}' is disabled. Enable the plugin with: "
                    f"mngr plugin enable {current_cfg.plugin}"
                )
            return
        if checked in config.disabled_plugins:
            raise MngrError(
                f"Agent type '{agent_type}' cannot be used because plugin "
                f"'{checked}' is disabled. Enable the plugin with: "
                f"mngr plugin enable {checked}"
            )
        if current_cfg is not None and current_cfg.parent_type is not None:
            checked = str(current_cfg.parent_type)
            current_cfg = config.agent_types.get(current_cfg.parent_type)
        else:
            checked = None


def resolve_agent_type(
    agent_type: AgentTypeName,
    config: MngrConfig,
) -> ResolvedAgentType:
    """Resolve an agent type name to its class and merged config.

    For custom types (defined in config with a parent_type), resolves through
    the parent type to get the correct agent class and config class, then
    applies the custom type's overrides on top of the parent type's
    user-configured settings (falling back to bare defaults if the parent
    type has no user config).

    For plugin-registered or direct command types, returns the registered
    class and config directly.

    Raises MngrError if the agent type (or its parent type) belongs to a
    disabled plugin.
    """
    _check_agent_type_not_disabled(agent_type, config)

    custom_config = config.agent_types.get(agent_type)

    if custom_config is not None and custom_config.parent_type is not None:
        parent_type = custom_config.parent_type
        agent_class = get_agent_class(str(parent_type))
        config_class = get_agent_config_class(str(parent_type))

        # Start from the parent type's user-configured settings (if any),
        # falling back to defaults. This ensures that e.g. [agent_types.claude]
        # is_fast = true is inherited by a child type with parent_type = "claude".
        parent_user_config = config.agent_types.get(parent_type)
        if parent_user_config is not None:
            parent_base_config = _apply_custom_overrides_to_parent_config(config_class(), parent_user_config)
        else:
            parent_base_config = config_class()
        merged_config = _apply_custom_overrides_to_parent_config(parent_base_config, custom_config)

        return ResolvedAgentType(
            agent_class=agent_class,
            agent_config=merged_config,
        )

    agent_class = get_agent_class(str(agent_type))
    config_class = get_agent_config_class(str(agent_type))

    if custom_config is not None:
        agent_config = custom_config
    else:
        agent_config = config_class()

    return ResolvedAgentType(
        agent_class=agent_class,
        agent_config=agent_config,
    )
