from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mngr.config.agent_alias_registry import normalize_agent_type_name
from imbue.mngr.config.agent_class_registry import get_agent_class
from imbue.mngr.config.agent_class_registry import is_agent_class_registered
from imbue.mngr.config.agent_plugin_registry import get_agent_type_owner
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.overlay_merge import merge_models_via_overlay
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UnknownAgentTypeError
from imbue.mngr.primitives import AgentTypeName


def _without_routing_metadata(config: AgentTypeConfig) -> AgentTypeConfig:
    """Return a copy of ``config`` with the inheritance-routing metadata
    (``parent_type`` / ``plugin``) cleared, so a parent/child merge never carries it into
    the merged runtime config. These two fields are routing metadata, not runtime config."""
    return config.model_copy_update(
        to_update(config.field_ref().parent_type, None),
        to_update(config.field_ref().plugin, None),
    )


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


def is_agent_config_registered(agent_type: str) -> bool:
    """Whether a specific config class is registered for this agent type."""
    return AgentTypeName(agent_type) in _agent_config_registry


def list_registered_agent_config_types() -> list[str]:
    """List all agent type names with registered config classes."""
    return sorted(str(k) for k in _agent_config_registry.keys())


def reset_agent_config_registry() -> None:
    """Reset the registry. Used for test isolation."""
    _agent_config_registry.clear()


def is_known_agent_type(agent_type: str, config: MngrConfig) -> bool:
    """Whether an agent type is known anywhere: a registered alias, a registered
    class, a registered config, or a user-defined [agent_types.X] block.

    This is the canonical predicate for "is this a real agent type name?" --
    callers should prefer this over checking individual registries. An alias
    is resolved to its canonical type before the registries are consulted.
    """
    canonical_type = normalize_agent_type_name(agent_type)
    name = AgentTypeName(canonical_type)
    return (
        name in config.agent_types
        or is_agent_class_registered(canonical_type)
        or is_agent_config_registered(canonical_type)
    )


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
    """Apply a custom agent type's overrides onto its parent type's config.

    The ``parent_type`` inheritance arm of the config merge (see ``config/README.md``):
    delegates to ``overlay_merge.merge_models_via_overlay`` with ``parent_config`` as the
    base, so the result reparses into ``type(parent_config)`` -- the class-switching crux,
    where a base-class ``custom_config`` folded onto a ``ClaudeAgentConfig`` parent yields
    a ``ClaudeAgentConfig`` with the parent's subclass-only fields. Both layers have their
    inheritance-routing metadata (``parent_type`` / ``plugin``) cleared first
    (``_without_routing_metadata``) so it never flows into the merged runtime config, and
    ``serialize_as_any`` keeps subclass-only fields. Assign-by-default, except
    ``SettingsPatchField`` fields accumulate across the boundary; cross-scope
    ``settings_overrides`` narrowing here is intentionally not surfaced (deferred with the
    broader narrowing-philosophy decision).
    """
    merged, _narrowings = merge_models_via_overlay(
        _without_routing_metadata(parent_config),
        _without_routing_metadata(custom_config),
        serialize_as_any=True,
    )
    return merged


def _check_agent_type_not_disabled(
    agent_type: AgentTypeName,
    config: MngrConfig,
) -> None:
    """Raise MngrError if the agent type or any ancestor in its parent chain is disabled.

    At each level, the plugin name to compare against ``disabled_plugins`` is
    resolved by this precedence: the explicit ``plugin`` field if set,
    otherwise the type's recorded owning plugin (the authoritative source --
    it knows the real registering plugin even when the type name differs from
    the entry-point name, e.g. ``pi-coding`` registered by the ``pi_coding``
    entry point), otherwise the type name itself.

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
        # Prefer the owning plugin recorded for this type; fall back to the
        # type name when nothing was recorded (e.g. config-only custom types).
        plugin_name = get_agent_type_owner(checked) or checked
        if plugin_name in config.disabled_plugins:
            raise MngrError(
                f"Agent type '{agent_type}' cannot be used because plugin "
                f"'{plugin_name}' is disabled. Enable the plugin with: "
                f"mngr plugin enable {plugin_name}"
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

    A name that is a registered alias is resolved to its canonical type first.
    If a custom ``[agent_types.X]`` block shares a name with an alias, the alias
    is dropped at config-load time so the custom type wins -- by the time this
    runs the name is no longer an alias and resolves to the custom type. A custom
    type's ``parent_type`` is normalized to canonical at config-load time too.

    Raises UnknownAgentTypeError if the agent type name is not known via any
    registry or user config (or, in the parent-type branch, if the parent
    type itself is not known). Raises MngrError if the agent type (or its
    parent type) belongs to a disabled plugin.
    """
    canonical_type = AgentTypeName(normalize_agent_type_name(agent_type))

    _check_agent_type_not_disabled(canonical_type, config)

    if not is_known_agent_type(str(canonical_type), config):
        raise UnknownAgentTypeError(str(canonical_type))

    custom_config = config.agent_types.get(canonical_type)

    if custom_config is not None and custom_config.parent_type is not None:
        parent_type = custom_config.parent_type
        if not is_known_agent_type(str(parent_type), config):
            raise UnknownAgentTypeError(str(parent_type))
        agent_class = get_agent_class(str(parent_type))
        config_class = get_agent_config_class(str(parent_type))

        # Start from the parent type's user-configured settings (if any),
        # falling back to defaults. This ensures that e.g. [agent_types.claude]
        # auto_dismiss_dialogs = true is inherited by a child type with parent_type = "claude".
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

    agent_class = get_agent_class(str(canonical_type))
    config_class = get_agent_config_class(str(canonical_type))

    if custom_config is not None:
        agent_config = custom_config
    else:
        agent_config = config_class()

    return ResolvedAgentType(
        agent_class=agent_class,
        agent_config=agent_config,
    )
