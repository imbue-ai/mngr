from typing import Any

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mng.config.agent_class_registry import get_agent_class
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import MngConfig
from imbue.mng.config.data_types import merge_cli_args
from imbue.mng.primitives import AgentTypeName

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
    concrete class with the base fields overridden.
    """
    updates: list[tuple[str, Any]] = []

    if custom_config.command is not None:
        updates.append(to_update(parent_config.field_ref().command, custom_config.command))

    merged_cli_args = merge_cli_args(parent_config.cli_args, custom_config.cli_args)
    if merged_cli_args != parent_config.cli_args:
        updates.append(to_update(parent_config.field_ref().cli_args, merged_cli_args))

    if custom_config.permissions:
        updates.append(to_update(parent_config.field_ref().permissions, custom_config.permissions))

    if not updates:
        return parent_config

    return parent_config.model_copy_update(*updates)


def resolve_agent_type(
    agent_type: AgentTypeName,
    config: MngConfig,
) -> ResolvedAgentType:
    """Resolve an agent type name to its class and merged config.

    For custom types (defined in config with a parent_type), resolves through
    the parent type to get the correct agent class and config class, then
    applies the custom type's overrides on top of the parent defaults.

    For plugin-registered or direct command types, returns the registered
    class and config directly.
    """
    custom_config = config.agent_types.get(agent_type)

    if custom_config is not None and custom_config.parent_type is not None:
        parent_type = custom_config.parent_type
        agent_class = get_agent_class(str(parent_type))
        config_class = get_agent_config_class(str(parent_type))

        parent_default_config = config_class()
        merged_config = _apply_custom_overrides_to_parent_config(parent_default_config, custom_config)

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
