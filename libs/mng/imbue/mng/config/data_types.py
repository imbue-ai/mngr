from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any
from typing import Final
from typing import Self
from typing import TypeVar
from uuid import uuid4

import pluggy
from pydantic import Field
from pydantic import GetCoreSchemaHandler
from pydantic import field_validator
from pydantic_core import CoreSchema
from pydantic_core import core_schema

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mng.errors import ConfigParseError
from imbue.mng.errors import ParseSpecError
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import LifecycleHook
from imbue.mng.primitives import OutputFormat
from imbue.mng.primitives import Permission
from imbue.mng.primitives import PluginName
from imbue.mng.primitives import ProviderBackendName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import UserId
from imbue.mng.utils.file_utils import atomic_write
from imbue.mng.utils.logging import LoggingConfig

USER_ID_FILENAME: Final[str] = "user_id"

# 7 days in seconds
_DEFAULT_DESTROYED_HOST_PERSISTED_SECONDS: Final[float] = 60.0 * 60.0 * 24.0 * 7.0

# === Helper Functions ===

T = TypeVar("T")


@pure
def split_cli_args_string(cli_args: str) -> tuple[str, ...]:
    """Split a CLI args string into individual argument tokens, preserving quoting.

    Uses shlex in non-POSIX mode so that quote characters (both single and double)
    are kept as part of the resulting tokens. This ensures that when the arguments
    are later joined with spaces (e.g. in assemble_command), the quoting is
    maintained and the resulting shell command is correct.

    Example:
        >>> split_cli_args_string("--settings '{\"key\": \"value\"}'")
        ('--settings', '\\'{"key": "value"}\\'')
    """
    lexer = shlex.shlex(cli_args, posix=False)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return tuple(lexer)


@pure
def merge_cli_args(base: tuple[str, ...], override: tuple[str, ...]) -> tuple[str, ...]:
    """Merge CLI arguments, concatenating if both present."""
    if override:
        return base + override
    return base


@pure
def merge_list_fields(base: list[T], override: list[T] | None) -> list[T]:
    """Merge list fields, concatenating if override is not None."""
    if override is not None:
        return list(base) + list(override)
    return base


K = TypeVar("K")
V = TypeVar("V")


@pure
def merge_dict_fields(base: dict[K, V], override: dict[K, V] | None) -> dict[K, V]:
    """Merge dict fields, with override keys taking precedence."""
    if override is not None:
        return {**base, **override}
    return base


# === Value Types ===


class EnvVar(FrozenModel):
    """Environment variable as KEY=VALUE."""

    key: str = Field(description="The environment variable name")
    value: str = Field(description="The environment variable value")

    @classmethod
    def from_string(cls, s: str) -> "EnvVar":
        """Parse a KEY=VALUE string into an EnvVar."""
        if "=" not in s:
            raise ParseSpecError(f"Environment variable must be in KEY=VALUE format, got: {s}")
        key, value = s.split("=", 1)
        return cls(key=key.strip(), value=value.strip())


class HookDefinition(FrozenModel):
    """Lifecycle hook definition as NAME:COMMAND."""

    hook: LifecycleHook = Field(description="The lifecycle hook name")
    command: str = Field(description="The command to run")

    @classmethod
    def from_string(cls, s: str) -> "HookDefinition":
        """Parse a NAME:COMMAND string into a HookDefinition."""
        if ":" not in s:
            raise ParseSpecError(f"Hook must be in NAME:COMMAND format, got: {s}")
        name, command = s.split(":", 1)
        # Normalize name: convert hyphens to underscores and uppercase
        normalized_name = name.strip().upper().replace("-", "_")
        try:
            hook = LifecycleHook(normalized_name)
        except ValueError:
            valid = ", ".join(h.value.lower().replace("_", "-") for h in LifecycleHook)
            raise ParseSpecError(f"Invalid hook name '{name}'. Valid hooks: {valid}") from None
        return cls(hook=hook, command=command.strip())


# === Config Types ===


class AgentTypeConfig(FrozenModel):
    """Defines a custom agent type that inherits from an existing type."""

    parent_type: AgentTypeName | None = Field(
        default=None,
        description="Base type to inherit from (must be a plugin-provided or command type, not another custom type)",
    )
    command: CommandString | None = Field(
        default=None,
        description="Command to run for this agent type",
    )
    cli_args: tuple[str, ...] = Field(
        default=(),
        description="Additional CLI arguments to pass to the agent",
    )
    permissions: list[Permission] = Field(
        default_factory=list,
        description="Explicit list of permissions (overrides parent type permissions)",
    )

    @field_validator("cli_args", mode="before")
    @classmethod
    def _normalize_cli_args(cls, value: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
        if isinstance(value, str):
            return split_cli_args_string(value) if value else ()
        return tuple(value)

    def merge_with(self, override: Self) -> Self:
        """Merge this config with an override config.

        Important note: despite the type signatures, any of these fields may be None in the override--this means that they were NOT set in the toml (and thus should be ignored)

        Scalar fields: override wins if not None
        Lists: concatenate both lists
        """
        # Ensure override is same type or subclass of self's type
        if not isinstance(override, self.__class__):
            raise ConfigParseError(f"Cannot merge {self.__class__.__name__} with different agent config type")

        # Merge parent_type (scalar - override wins if not None)
        merged_parent_type = override.parent_type if override.parent_type is not None else self.parent_type

        # Merge command (scalar - override wins if not None)
        merged_command = override.command if override.command is not None else self.command

        # Merge cli_args (concatenate both tuples)
        merged_cli_args = merge_cli_args(self.cli_args, override.cli_args)

        # Merge permissions (list - concatenate if override is not None)
        merged_permissions = merge_list_fields(self.permissions, override.permissions)

        return self.__class__(
            parent_type=merged_parent_type,
            command=merged_command,
            cli_args=merged_cli_args,
            permissions=merged_permissions,
        )


class ProviderInstanceConfig(FrozenModel):
    """Defines a custom provider instance."""

    backend: ProviderBackendName = Field(
        description="Provider backend to use (e.g., 'docker', 'modal', 'aws')",
    )
    plugin: str | None = Field(
        default=None,
        description="Plugin that provides this backend. Defaults to the backend name. "
        "Used to skip parsing when the plugin is disabled.",
    )
    is_enabled: bool | None = Field(
        default=None,
        description="Whether this provider instance is enabled. Set to false to disable without removing configuration.",
    )
    destroyed_host_persisted_seconds: float | None = Field(
        default=None,
        description="How long (in seconds) a destroyed host's records are kept before permanent deletion. "
        "Overrides the global default_destroyed_host_persisted_seconds when set.",
    )

    def merge_with(self, override: "ProviderInstanceConfig") -> "ProviderInstanceConfig":
        """Merge this config with an override config.

        Important note: despite the type signatures, any of these fields may be None in the override--this means that they were NOT set in the toml (and thus should be ignored)

        Scalar fields: override wins if not None
        List fields: concatenate both lists
        Dict fields: merge keys, with override keys taking precedence
        """
        # Ensure override is same type as self
        if not isinstance(override, self.__class__):
            raise ConfigParseError(f"Cannot merge {self.__class__.__name__} with different provider config type")

        # Merge all fields: for each field, use appropriate merge strategy based on type
        # Backend always comes from override
        merged_values: dict[str, Any] = {}
        for field_name in self.__class__.model_fields:
            if field_name == "backend":
                merged_values[field_name] = override.backend
            else:
                base_value = getattr(self, field_name)
                override_value = getattr(override, field_name)
                if isinstance(base_value, list):
                    # Lists: concatenate
                    merged_values[field_name] = merge_list_fields(base_value, override_value)
                elif isinstance(base_value, dict):
                    # Dicts: merge keys with override taking precedence
                    merged_values[field_name] = merge_dict_fields(base_value, override_value)
                elif override_value is not None:
                    # Scalars: override wins if not None
                    merged_values[field_name] = override_value
                else:
                    merged_values[field_name] = base_value
        return self.__class__(**merged_values)


class PluginConfig(FrozenModel):
    """Base configuration for a plugin."""

    enabled: bool = Field(
        default=True,
        description="Whether this plugin is enabled",
    )

    def merge_with(self, override: "PluginConfig") -> "PluginConfig":
        """Merge this config with an override config.

        Important note: despite the type signatures, any of these fields may be None in the override--this means that they were NOT set in the toml (and thus should be ignored)

        Scalar fields: override wins if not None
        """
        merged_enabled = override.enabled if override.enabled is not None else self.enabled
        return self.__class__(enabled=merged_enabled)


class CommandDefaults(FrozenModel):
    """Default values for CLI command parameters.

    This allows config files to override default values for CLI arguments.
    Only parameters that were not explicitly set by the user will use these defaults.
    Field names should match the CLI parameter names (after click's conversion).
    """

    # Store as a flexible dict since we don't know all possible CLI parameters ahead of time
    defaults: dict[str, Any] = Field(
        default_factory=dict,
        description="Map of parameter name to default value",
    )
    default_subcommand: str | None = Field(
        default=None,
        description="Default subcommand when this group is invoked with no recognized command. "
        "Empty string disables defaulting (shows help instead).",
    )

    def merge_with(self, override: Self) -> Self:
        """Merge this config with an override config.

        Important note: despite the type signatures, any of these fields may be None in the override--this means that they were NOT set in the toml (and thus should be ignored)

        For command defaults, later configs completely override earlier ones.
        default_subcommand: scalar, override wins if not None.
        """
        merged_defaults = {**self.defaults, **override.defaults}
        merged_default_subcommand = (
            override.default_subcommand if override.default_subcommand is not None else self.default_subcommand
        )
        return self.__class__(defaults=merged_defaults, default_subcommand=merged_default_subcommand)


class CreateTemplateName(str):
    """Name of a create template."""

    def __new__(cls, value: str) -> Self:
        if not value:
            raise ParseSpecError("Template name cannot be empty")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(min_length=1),
            serialization=core_schema.to_string_ser_schema(),
        )


class CreateTemplate(FrozenModel):
    """Template for the create command.

    Templates are named presets of create command arguments that can be applied
    using --template <name>. All fields are optional; only specified fields
    will override the defaults when the template is applied.

    Templates are useful for setting up common configurations for different
    providers or environments (e.g., different paths in remote containers vs locally).
    """

    # Store as a flexible dict since templates can contain any create command parameter
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="Map of parameter name to value for create command options",
    )

    def merge_with(self, override: Self) -> Self:
        """Merge this template with an override template.

        Important note: despite the type signatures, any of these fields may be None in the override--this means that they were NOT set in the toml (and thus should be ignored)

        For templates, later configs override earlier ones on a per-key basis.
        """
        merged_options = {**self.options, **override.options}
        return self.__class__(options=merged_options)


class MngConfig(FrozenModel):
    """Root configuration model for mng."""

    prefix: str = Field(
        default="mng-",
        description="Prefix for naming resources (tmux sessions, containers, etc.)",
    )
    default_host_dir: Path = Field(
        default=Path("~/.mng"),
        description="Default base directory for mng data on hosts (can be overridden per provider instance)",
    )
    unset_vars: list[str] = Field(
        # these are necessary to prevent tmux from accidentally sticking test data in history files
        default_factory=lambda: list(("HISTFILE", "PROFILE", "VIRTUAL_ENV")),
        description="Environment variables to unset when creating agent tmux sessions",
    )
    pager: str | None = Field(
        default=None,
        description="Pager command for help output (e.g., 'less'). If None, uses PAGER env var or 'less' as fallback.",
    )
    enabled_backends: list[ProviderBackendName] = Field(
        default_factory=list,
        description="List of enabled provider backends. If empty, all backends are enabled. If non-empty, only the listed backends are enabled.",
    )
    agent_types: dict[AgentTypeName, AgentTypeConfig] = Field(
        default_factory=dict,
        description="Custom agent type definitions",
    )
    providers: dict[ProviderInstanceName, ProviderInstanceConfig] = Field(
        default_factory=dict,
        description="Custom provider instance definitions",
    )
    plugins: dict[PluginName, PluginConfig] = Field(
        default_factory=dict,
        description="Plugin configurations",
    )
    disabled_plugins: frozenset[str] = Field(
        default_factory=frozenset,
        description="Set of plugin names that were explicitly disabled (used to filter backends)",
    )
    commands: dict[str, CommandDefaults] = Field(
        default_factory=dict,
        description="Default values for CLI command parameters (e.g., 'commands.create')",
    )
    create_templates: dict[CreateTemplateName, CreateTemplate] = Field(
        default_factory=dict,
        description="Named templates for the create command (e.g., 'create_templates.modal-dev')",
    )
    pre_command_scripts: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Commands to run before CLI commands execute, keyed by command name (e.g., 'create': ['echo hello', 'validate.sh'])",
    )
    logging: LoggingConfig = Field(
        default_factory=LoggingConfig,
        description="Logging configuration",
    )
    is_remote_agent_installation_allowed: bool = Field(
        default=True,
        description="Whether to allow automatic installation of agents (e.g. Claude) on remote hosts. "
        "When False, raises an error if the agent is not already installed on the remote host. "
        "Defaults to True (allowed).",
    )
    connect_command: str | None = Field(
        default=None,
        description="Custom command to run instead of the builtin connect when create or start connects to agents. "
        "The environment variables MNG_AGENT_NAME and MNG_SESSION_NAME are set before running the command.",
    )
    is_nested_tmux_allowed: bool = Field(
        default=False,
        description="Allow attaching to tmux sessions from within an existing tmux session by unsetting $TMUX",
    )
    is_error_reporting_enabled: bool = Field(
        default=True,
        description="Whether to prompt users to report unexpected errors as GitHub issues when running interactively",
    )
    is_allowed_in_pytest: bool = Field(
        default=True,
        description="Set this to False to prevent loading this config in pytest runs",
    )
    default_destroyed_host_persisted_seconds: float = Field(
        default=_DEFAULT_DESTROYED_HOST_PERSISTED_SECONDS,
        description="Default number of seconds a destroyed host's records are kept before permanent deletion. "
        "Can be overridden per provider via destroyed_host_persisted_seconds in the provider config.",
    )

    def merge_with(self, override: Self) -> Self:
        """Merge this config with an override config.

        Important note: despite the type signatures, any of these fields may be None in the override--this means that they were NOT set in the toml (and thus should be ignored)

        Scalar fields: override wins if not None
        Dicts: merge keys, with per-key merge for nested config objects
        Lists: concatenate both lists
        """
        # Merge prefix (scalar - override wins if not None)
        merged_prefix = self.prefix
        if override.prefix is not None:
            merged_prefix = override.prefix

        # Merge default_host_dir (scalar - override wins if not None)
        merged_default_host_dir = self.default_host_dir
        if override.default_host_dir is not None:
            merged_default_host_dir = override.default_host_dir

        # Merge pager (scalar - override wins if not None)
        merged_pager = override.pager if override.pager is not None else self.pager

        # Merge unset_vars (list - concatenate)
        merged_unset_vars = list(self.unset_vars) + list(override.unset_vars)

        # Merge enabled_backends (list - override wins if not empty, otherwise keep base)
        merged_enabled_backends = override.enabled_backends if override.enabled_backends else self.enabled_backends

        # Merge agent_types (dict - merge keys, with per-key merge)
        merged_agent_types: dict[AgentTypeName, AgentTypeConfig] = {}
        all_type_keys = set(self.agent_types.keys()) | set(override.agent_types.keys())
        for key in all_type_keys:
            if key in self.agent_types and key in override.agent_types:
                # Both have this key - merge the configs
                merged_agent_types[key] = self.agent_types[key].merge_with(override.agent_types[key])
            elif key in override.agent_types:
                # Only override has this key
                merged_agent_types[key] = override.agent_types[key]
            else:
                # Only base has this key
                merged_agent_types[key] = self.agent_types[key]

        # Merge providers (dict - merge keys, with per-key merge)
        merged_providers: dict[ProviderInstanceName, ProviderInstanceConfig] = {}
        all_provider_keys = set(self.providers.keys()) | set(override.providers.keys())
        for key in all_provider_keys:
            if key in self.providers and key in override.providers:
                # Both have this key - merge the configs
                merged_providers[key] = self.providers[key].merge_with(override.providers[key])
            elif key in override.providers:
                # Only override has this key
                merged_providers[key] = override.providers[key]
            else:
                # Only base has this key
                merged_providers[key] = self.providers[key]

        # Merge plugins (dict - merge keys, with per-key merge)
        merged_plugins: dict[PluginName, PluginConfig] = {}
        all_plugin_keys = set(self.plugins.keys()) | set(override.plugins.keys())
        for key in all_plugin_keys:
            if key in self.plugins and key in override.plugins:
                # Both have this key - merge the configs
                merged_plugins[key] = self.plugins[key].merge_with(override.plugins[key])
            elif key in override.plugins:
                # Only override has this key
                merged_plugins[key] = override.plugins[key]
            else:
                # Only base has this key
                merged_plugins[key] = self.plugins[key]

        # Merge disabled_plugins (union of both sets)
        merged_disabled_plugins = self.disabled_plugins | override.disabled_plugins

        # Merge commands (dict - merge keys, with per-key merge)
        merged_commands: dict[str, CommandDefaults] = {}
        all_command_keys = set(self.commands.keys()) | set(override.commands.keys())
        for key in all_command_keys:
            if key in self.commands and key in override.commands:
                # Both have this key - merge the configs
                merged_commands[key] = self.commands[key].merge_with(override.commands[key])
            elif key in override.commands:
                # Only override has this key
                merged_commands[key] = override.commands[key]
            else:
                # Only base has this key
                merged_commands[key] = self.commands[key]

        # Merge create_templates (dict - merge keys, with per-key merge)
        merged_create_templates: dict[CreateTemplateName, CreateTemplate] = {}
        all_template_keys = set(self.create_templates.keys()) | set(override.create_templates.keys())
        for key in all_template_keys:
            if key in self.create_templates and key in override.create_templates:
                # Both have this key - merge the templates
                merged_create_templates[key] = self.create_templates[key].merge_with(override.create_templates[key])
            elif key in override.create_templates:
                # Only override has this key
                merged_create_templates[key] = override.create_templates[key]
            else:
                # Only base has this key
                merged_create_templates[key] = self.create_templates[key]

        # Merge pre_command_scripts (dict - override keys take precedence)
        merged_pre_command_scripts = merge_dict_fields(self.pre_command_scripts, override.pre_command_scripts)

        is_remote_agent_installation_allowed = self.is_remote_agent_installation_allowed
        if override.is_remote_agent_installation_allowed is not None:
            is_remote_agent_installation_allowed = override.is_remote_agent_installation_allowed

        # Merge connect_command (scalar - override wins if not None)
        merged_connect_command = (
            override.connect_command if override.connect_command is not None else self.connect_command
        )

        # Merge is_nested_tmux_allowed (scalar - override wins if not None)
        merged_is_nested_tmux_allowed = self.is_nested_tmux_allowed
        if override.is_nested_tmux_allowed is not None:
            merged_is_nested_tmux_allowed = override.is_nested_tmux_allowed

        # Merge is_error_reporting_enabled (scalar - override wins if not None)
        merged_is_error_reporting_enabled = self.is_error_reporting_enabled
        if override.is_error_reporting_enabled is not None:
            merged_is_error_reporting_enabled = override.is_error_reporting_enabled

        is_allowed_in_pytest = self.is_allowed_in_pytest
        if override.is_allowed_in_pytest is not None:
            is_allowed_in_pytest = override.is_allowed_in_pytest

        # Merge default_destroyed_host_persisted_seconds (scalar - override wins if not None)
        default_destroyed_host_persisted_seconds = self.default_destroyed_host_persisted_seconds
        if override.default_destroyed_host_persisted_seconds is not None:
            default_destroyed_host_persisted_seconds = override.default_destroyed_host_persisted_seconds

        # Merge logging (nested config - use merge_with if override.logging is not None)
        merged_logging = self.logging
        if override.logging is not None:
            merged_logging = self.logging.merge_with(override.logging)

        return self.__class__(
            prefix=merged_prefix,
            default_host_dir=merged_default_host_dir,
            pager=merged_pager,
            unset_vars=merged_unset_vars,
            enabled_backends=merged_enabled_backends,
            agent_types=merged_agent_types,
            providers=merged_providers,
            plugins=merged_plugins,
            disabled_plugins=merged_disabled_plugins,
            commands=merged_commands,
            create_templates=merged_create_templates,
            pre_command_scripts=merged_pre_command_scripts,
            is_remote_agent_installation_allowed=is_remote_agent_installation_allowed,
            connect_command=merged_connect_command,
            logging=merged_logging,
            is_nested_tmux_allowed=merged_is_nested_tmux_allowed,
            is_error_reporting_enabled=merged_is_error_reporting_enabled,
            is_allowed_in_pytest=is_allowed_in_pytest,
            default_destroyed_host_persisted_seconds=default_destroyed_host_persisted_seconds,
        )


class MngContext(FrozenModel):
    """Context object containing configuration and plugin manager.

    This combines MngConfig and PluginManager into a single object
    that can be passed through the application, providing access to
    both configuration and plugin hooks.
    """

    model_config = {"arbitrary_types_allowed": True}

    config: MngConfig = Field(
        description="Configuration for mng",
    )
    pm: pluggy.PluginManager = Field(
        description="Plugin manager for hooks and backends",
    )
    is_interactive: bool = Field(
        default=False,
        description="Whether the CLI is running in interactive mode (can prompt user for input)",
    )
    is_auto_approve: bool = Field(
        default=False,
        description="Whether to auto-approve prompts (e.g., skill installation) without asking",
    )
    profile_dir: Path = Field(
        description="Profile-specific directory for user data (user_id, providers, settings)",
    )
    concurrency_group: ConcurrencyGroup = Field(
        default_factory=lambda: ConcurrencyGroup(name="default"),
        description="Top-level concurrency group for managing spawned processes",
    )

    def get_profile_user_id(self) -> UserId:
        return get_or_create_user_id(self.profile_dir)


class OutputOptions(FrozenModel):
    """Options for command output formatting."""

    output_format: OutputFormat = Field(
        default=OutputFormat.HUMAN,
        description="Output format for command results",
    )
    format_template: str | None = Field(
        default=None,
        description="Format template string for custom output formatting (set when --format is a template string rather than a built-in format name)",
    )
    is_quiet: bool = Field(
        default=False,
        description="Whether to suppress all stdout output (set by --quiet)",
    )


def get_or_create_user_id(profile_dir: Path) -> UserId:
    """Get or create a unique user ID for this mng profile.

    The user ID is stored in a file in the profile directory. This ID is used
    to namespace Modal apps, ensuring that sandboxes created by different mng
    installations on a shared Modal account don't interfere with each other.
    """
    user_id_file = profile_dir / USER_ID_FILENAME

    if user_id_file.exists():
        user_id = user_id_file.read_text().strip()
        if os.environ.get("MNG_USER_ID", ""):
            assert user_id == os.environ.get("MNG_USER_ID", ""), (
                "MNG_USER_ID environment variable does not match existing user ID file"
            )
    else:
        if os.environ.get("MNG_USER_ID", ""):
            user_id = os.environ.get("MNG_USER_ID", "")
        else:
            # Generate a new user ID
            user_id = uuid4().hex
        atomic_write(user_id_file, user_id)
    return UserId(user_id)
