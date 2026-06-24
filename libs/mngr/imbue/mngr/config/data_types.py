from __future__ import annotations

import os
import shlex
from enum import auto
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import Final
from typing import Self
from typing import TypeVar
from uuid import uuid4

import pluggy
from pydantic import AfterValidator
from pydantic import Field
from pydantic import GetCoreSchemaHandler
from pydantic import field_validator
from pydantic_core import CoreSchema
from pydantic_core import core_schema

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.config.field_markers import RegistryField
from imbue.mngr.config.overlay_merge import merge_models_via_overlay
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.errors import ParseSpecError
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import LifecycleHook
from imbue.mngr.primitives import NewAgentLocation
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import PluginName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import UserId
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.logging import LoggingConfig
from imbue.overlay.markers import ScalarTuple

USER_ID_FILENAME: Final[str] = "user_id"

# 7 days in seconds -- controls how long destroyed host records (and their
# snapshots) are kept before permanent deletion, giving users a recovery
# window via `mngr create --snapshot`.
_DEFAULT_DESTROYED_HOST_PERSISTED_SECONDS: Final[float] = 60.0 * 60.0 * 24.0 * 7.0

# 10 minutes -- minimum age before GC will destroy an online host with no agents.
# Short because we only need to protect against transient empty states (e.g. between
# agent creation and discovery).
_DEFAULT_MIN_ONLINE_HOST_AGE_SECONDS: Final[float] = 60.0 * 10.0

# === Helper Functions ===

PluginConfigT = TypeVar("PluginConfigT", bound="PluginConfig")


def _coerce_to_scalar_tuple(value: tuple[str, ...]) -> ScalarTuple:
    """After-validator for ``ScalarStrTuple``: wrap the validated string tuple in
    ``ScalarTuple`` so the settings-narrowing guard treats the field as
    replace-by-default."""
    return ScalarTuple(value)


# A ``tuple[str, ...]`` config field that is semantically a single scalar value: a
# higher-precedence settings layer that sets it replaces the whole value rather than
# narrowing it. Use for fields where combining entries across config layers is never
# the intent (e.g. an AWS provider's ``allowed_ssh_cidrs``). The narrowing exemption
# only takes effect under ``model_validate`` (which runs the after-validator);
# ``_parse_providers`` validates provider blocks, so provider-config fields qualify.
ScalarStrTuple = Annotated[tuple[str, ...], AfterValidator(_coerce_to_scalar_tuple)]


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


# === Enums ===


class WorkDirExtraPathMode(UpperCaseStrEnum):
    """Transfer mode for extra paths in new work directories."""

    SHARE = auto()
    COPY = auto()


class ConfigScope(UpperCaseStrEnum):
    """A settings-file layer: the user profile, the project, or the local override.

    The lowercased member name is the value accepted by ``mngr config set
    --scope`` and surfaced in diagnostics; ``get_config_path`` (cli/config.py)
    and ``read_config_layers`` (pre_readers.py) both map these to the same files.
    """

    USER = auto()
    PROJECT = auto()
    LOCAL = auto()


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
    plugin: str | None = Field(
        default=None,
        description="Plugin that provides this agent type. Defaults to parent_type (if set) or the type name. "
        "Used to skip parsing when the plugin is disabled.",
    )
    command: CommandString | None = Field(
        default=None,
        description="Command to run for this agent type",
    )
    cli_args: tuple[str, ...] = Field(
        default=(),
        description="Additional CLI arguments to pass to the agent",
    )
    extra_provision_command: tuple[str, ...] = Field(
        default=(),
        description="Shell commands to run during provisioning",
    )
    upload_file: tuple[str, ...] = Field(
        default=(),
        description="LOCAL:REMOTE file upload specs",
    )
    create_directory: tuple[str, ...] = Field(
        default=(),
        description="Directories to create on the remote",
    )
    env: tuple[str, ...] = Field(
        default=(),
        description="KEY=VALUE environment variables",
    )
    env_file: tuple[str, ...] = Field(
        default=(),
        description="Paths to env files",
    )

    @field_validator("cli_args", mode="before")
    @classmethod
    def _normalize_cli_args(cls, value: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
        if isinstance(value, str):
            return split_cli_args_string(value) if value else ()
        return tuple(value)


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
    min_online_host_age_seconds: float | None = Field(
        default=None,
        description="Minimum age (in seconds) before GC will destroy an online host with no agents. "
        "Overrides the global default_min_online_host_age_seconds when set.",
    )


class PluginConfig(FrozenModel):
    """Base configuration for a plugin."""

    enabled: bool = Field(
        default=True,
        description="Whether this plugin is enabled",
    )


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


class RetryConfig(FrozenModel):
    """Configuration for connection retry behavior.

    Controls how many times and how frequently mngr retries SSH connections
    to remote agents when connecting (via both ``mngr create --connect`` and
    ``mngr connect``).
    """

    connect_retry_times: int = Field(
        default=3,
        description="Number of times to retry a failed SSH connection before giving up",
    )
    connect_retry_delay: str = Field(
        default="5s",
        description="Delay between connection retries (e.g., '5s', '1m')",
    )


class TmuxConfig(FrozenModel):
    """Configuration for the tmux sessions that mngr runs agents in.

    These options let tmux users customize how mngr creates and attaches to agent
    sessions without resorting to a full ``connect_command`` override. See
    ``docs/tmux_users.md`` for usage.
    """

    primary_window_name: str = Field(
        default="agent",
        min_length=1,
        description="Name of the primary tmux window where the agent runs. mngr targets this "
        "window by name rather than by index (``:0``), so its targeting works regardless of the "
        "user's tmux 'base-index' setting.",
    )
    attach_args: tuple[str, ...] = Field(
        default=(),
        description="Extra tmux client flags inserted before the 'attach' subcommand when "
        "connecting to an agent, i.e. ``tmux <attach_args> attach ...``. For example, "
        "['-CC'] enables iTerm2 control mode; '-u' / '-2' force UTF-8 / 256-color.",
    )
    additional_config_path: Path | None = Field(
        default=None,
        description="Path (on the agent's host) to an additional tmux config file sourced into "
        "every mngr session. Unlike the auto-generated ~/.mngr/tmux.conf, this file is never "
        "overwritten by mngr, so it is a stable place for mngr-session-specific tmux config.",
    )

    @field_validator("attach_args", mode="before")
    @classmethod
    def _normalize_attach_args(cls, value: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
        if isinstance(value, str):
            return split_cli_args_string(value) if value else ()
        return tuple(value)

    def merge_with(self, override: Self) -> Self:
        """Merge this config with an override config.

        Uses ``model_fields_set`` so only the fields the override layer actually
        set replace the base value. ``attach_args`` is assign-by-default like
        other aggregate fields; use ``tmux.attach_args__extend`` in TOML for
        additive behavior.
        """
        explicitly_set = override.model_fields_set
        if not explicitly_set:
            return self
        override_values = override.model_dump()
        updates: list[tuple[str, Any]] = [(field_name, override_values[field_name]) for field_name in explicitly_set]
        return self.model_copy_update(*updates)


class MngrConfig(FrozenModel):
    """Root configuration model for mngr."""

    prefix: str = Field(
        default="mngr-",
        description="Prefix for naming resources (tmux sessions, containers, etc.)",
    )
    default_host_dir: Path = Field(
        default=Path("~/.mngr"),
        description="Default base directory for mngr data on hosts (can be overridden per provider instance)",
    )
    unset_vars: list[str] = Field(
        # these are necessary to prevent tmux from accidentally sticking test data in history files
        default_factory=lambda: list(("HISTFILE", "PROFILE", "VIRTUAL_ENV")),
        description="Environment variables to unset when creating agent tmux sessions",
    )
    work_dir_extra_paths: dict[str, WorkDirExtraPathMode] = Field(
        default_factory=dict,
        description="Paths to transfer into new work directories, mapped to transfer mode. "
        "'SHARE': symlink on same host, copy on different host. "
        "'COPY': always copy via rsync.",
    )
    pager: str | None = Field(
        default=None,
        description="Pager command for help output (e.g., 'less'). If None, uses PAGER env var or 'less' as fallback.",
    )
    enabled_backends: list[ProviderBackendName] = Field(
        default_factory=list,
        description="List of enabled provider backends. If empty, all backends are enabled. If non-empty, only the listed backends are enabled.",
    )
    agent_types: Annotated[dict[AgentTypeName, AgentTypeConfig], RegistryField()] = Field(
        default_factory=dict,
        description="Custom agent type definitions",
    )
    providers: Annotated[dict[ProviderInstanceName, ProviderInstanceConfig], RegistryField()] = Field(
        default_factory=dict,
        description="Custom provider instance definitions",
    )
    plugins: Annotated[dict[PluginName, PluginConfig], RegistryField()] = Field(
        default_factory=dict,
        description="Plugin configurations",
    )
    disabled_plugins: frozenset[str] = Field(
        default_factory=frozenset,
        description="Set of plugin names that were explicitly disabled (used to filter backends)",
    )
    commands: Annotated[dict[str, CommandDefaults], RegistryField()] = Field(
        default_factory=dict,
        description="Default values for CLI command parameters (e.g., 'commands.create')",
    )
    create_templates: Annotated[dict[CreateTemplateName, CreateTemplate], RegistryField()] = Field(
        default_factory=dict,
        description="Named templates for the create command (e.g., 'create_templates.modal-dev')",
    )
    pre_command_scripts: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Commands to run before CLI commands execute, keyed by command name (e.g., 'create': ['echo hello', 'validate.sh'])",
    )
    retry: RetryConfig = Field(
        default_factory=RetryConfig,
        description="Connection retry configuration",
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
        description="Custom command to run instead of the builtin connect when create, start, or connect connects to agents. "
        "The environment variables MNGR_AGENT_NAME and MNGR_SESSION_NAME are set before running the command.",
    )
    is_nested_tmux_allowed: bool = Field(
        default=False,
        description="Allow attaching to tmux sessions from within an existing tmux session by unsetting $TMUX",
    )
    tmux: TmuxConfig = Field(
        default_factory=TmuxConfig,
        description="Configuration for the tmux sessions that mngr runs agents in "
        "(primary window name, attach flags such as -CC, extra sourced config file).",
    )
    headless: bool = Field(
        default=False,
        description="When true, disables all interactive behavior (prompts, TUI, editor). "
        "Equivalent to passing --headless on the CLI. Can also be set via MNGR_HEADLESS env var.",
    )
    is_error_reporting_enabled: bool = Field(
        default=True,
        description="Whether to suggest launching a diagnostic agent "
        "when an unexpected error occurs while running interactively",
    )
    is_allowed_in_pytest: bool = Field(
        default=False,
        description=(
            "Whether this config may be loaded during a pytest run. Defaults to False so a "
            "poorly-scoped test cannot pick up a real config (e.g. ~/.mngr) and perform real "
            "operations; configs written for tests set this to True to opt in."
        ),
    )
    default_destroyed_host_persisted_seconds: float = Field(
        default=_DEFAULT_DESTROYED_HOST_PERSISTED_SECONDS,
        description="Default number of seconds a destroyed host's records are kept before permanent deletion. "
        "Can be overridden per provider via destroyed_host_persisted_seconds in the provider config.",
    )
    default_min_online_host_age_seconds: float = Field(
        default=_DEFAULT_MIN_ONLINE_HOST_AGE_SECONDS,
        description="Default minimum age (in seconds) before GC will destroy an online host with no agents. "
        "Can be overridden per provider via min_online_host_age_seconds in the provider config.",
    )
    agent_ready_timeout: float = Field(
        default=10.0,
        description="Max seconds to wait for an agent to signal readiness before sending messages. "
        "Hook-based polling returns early; this is an upper bound, not an unconditional delay.",
    )
    # Consider removing this global flag entirely: the per-key `__assign` suffix now gives a
    # targeted opt-out from the narrowing guard, which may make a blanket escape hatch
    # unnecessary. (The once-planned flip of this default to True is no longer planned.)
    allow_settings_key_assignment_narrowing: bool = Field(
        default=False,
        description=(
            "When False (the default), it is an error for a higher-precedence settings layer "
            "(project local, env vars, --setting, etc.) to assign over a non-empty list/tuple/"
            "dict/set value coming from a lower-precedence layer. This guards against silently "
            "losing entries when a settings file is loaded with the assign-by-default merge "
            "behavior; the user is told to use the __extend suffix to keep the additive behavior, "
            "the __assign suffix to replace a specific key without this error, or to set this "
            "field to True to allow assign-by-default narrowing globally."
        ),
    )

    def agent_session_name(self, agent_name: str) -> str:
        """The tmux session name for an agent: the configured ``prefix`` + the agent name.

        Single source of truth for the ``prefix + name`` rule, so call sites do not
        hand-roll the f-string (and so cannot drift from one another).
        """
        return f"{self.prefix}{agent_name}"

    def merge_with(self, override: Self) -> tuple[Self, list[str]]:
        """Merge this config with an override config (the loader's whole-config merge),
        returning the merged config and every narrowing path the overlay merge surfaced.

        Assign-by-default for aggregate fields; the top-level container dicts
        (``agent_types``, ``providers``, ``plugins``, ``commands``, ``create_templates``)
        merge per-key, and ``SettingsPatchField`` fields accumulate. Computed via the
        overlay pipeline, behavior-identical to the old field-by-field merge; see
        ``config/README.md`` for the scheme.

        The narrowings are the single config-load narrowing detector: cross-scope
        bare-drops of a non-empty aggregate by a higher-precedence layer -- both ordinary
        assign-by-default field drops (e.g. ``agent_types.<name>.cli_args``,
        ``commands.create.defaults.env``) and ``SettingsPatchField`` drops *inside* an
        accumulating settings patch (e.g. ``agent_types.<name>.settings_overrides.<key>...``).
        ``Static*`` atomic aggregates are exempt via the override-side re-marking. The loader
        routes the whole list into its flag-gated narrowing aggregation; callers that only
        need the merged value drop the second element explicitly.
        """
        return merge_models_via_overlay(
            self,
            override,
            serialize_as_any=True,
            drop_none_values=True,
        )


class MngrContext(FrozenModel):
    """Context object containing configuration and plugin manager.

    This combines MngrConfig and PluginManager into a single object
    that can be passed through the application, providing access to
    both configuration and plugin hooks.
    """

    model_config = {"arbitrary_types_allowed": True}

    config: MngrConfig = Field(
        description="Configuration for mngr",
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
    is_full_discovery: bool = Field(
        default=False,
        description="When True, always query all providers during discovery (skip event-stream optimization)",
    )
    project_root: Path | None = Field(
        default=None,
        description="Project root directory (git worktree root)",
    )

    def get_plugin_config(self, name: str, config_type: type[PluginConfigT]) -> PluginConfigT:
        """Get a plugin's typed config, falling back to defaults if absent."""
        config = self.config.plugins.get(PluginName(name))
        if config is None:
            return config_type()
        if not isinstance(config, config_type):
            raise ConfigParseError(
                f"Plugin '{name}' config has type {type(config).__name__}, expected {config_type.__name__}"
            )
        return config

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
    """Get or create a unique user ID for this mngr profile.

    The user ID is stored in a file in the profile directory. This ID is used
    to namespace Modal apps, ensuring that sandboxes created by different mngr
    installations on a shared Modal account don't interfere with each other.
    """
    user_id_file = profile_dir / USER_ID_FILENAME

    if user_id_file.exists():
        user_id = user_id_file.read_text().strip()
        if os.environ.get("MNGR_USER_ID", ""):
            assert user_id == os.environ.get("MNGR_USER_ID", ""), (
                "MNGR_USER_ID environment variable does not match existing user ID file"
            )
    else:
        if os.environ.get("MNGR_USER_ID", ""):
            user_id = os.environ.get("MNGR_USER_ID", "")
        else:
            # Generate a new user ID
            user_id = uuid4().hex
        atomic_write(user_id_file, user_id)
    return UserId(user_id)


class CommonCliOptions(FrozenModel):
    """Base class for common CLI options shared across all commands.

    This captures the options added by the @add_common_options decorator.
    All command-specific option classes should inherit from this class.

    Note that this class VERY INTENTIONALLY DOES NOT use Field() decorators with descriptions, defaults, etc.
    For that information, see the @add_common_options decorator and its click.option() decorators.
    """

    headless: bool = False
    safe: bool = False
    output_format: str
    quiet: bool
    verbose: int
    log_file: str | None
    log_commands: bool | None
    plugin: tuple[str, ...]
    disable_plugin: tuple[str, ...]
    setting: tuple[str, ...] = ()


class CreateCliOptions(CommonCliOptions):
    """Options passed from the CLI to the create command.

    This captures all the click parameters so we can pass them as a single object
    to helper functions instead of passing dozens of individual parameters.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.

    Note that this class VERY INTENTIONALLY DOES NOT use Field() decorators with descriptions, defaults, etc.
    For that information, see the click.option() and click.argument() decorators on the create() function itself.
    """

    positional_name: NewAgentLocation | None
    positional_agent_type: str | None
    agent_args: tuple[str, ...]
    template: tuple[str, ...]
    type: str | None
    reuse: bool
    connect: bool
    foreground: bool
    connect_command: str | None
    ensure_clean: bool
    name: NewAgentLocation | None
    id: str | None
    name_style: str
    extra_window: tuple[str, ...]
    source: str | None
    adopt_session: tuple[str, ...]
    target_path: str | None
    transfer: str | None
    rsync: bool | None
    rsync_args: str | None
    include_unclean: bool | None
    include_gitignored: bool
    branch: str
    env: tuple[str, ...]
    env_file: tuple[str, ...]
    pass_env: tuple[str, ...]
    provider: str | None
    new_host: bool
    host_name_style: str
    host_label: tuple[str, ...]
    label: tuple[str, ...]
    project: str | None
    host_env: tuple[str, ...]
    host_env_file: tuple[str, ...]
    pass_host_env: tuple[str, ...]
    snapshot: str | None
    build_arg: tuple[str, ...]
    start_arg: tuple[str, ...]
    post_host_create_command: tuple[str, ...]
    post_host_create_outer_command: tuple[str, ...]
    reconnect: bool
    message: str | None
    message_file: str | None
    edit_message: bool
    session_command: str | None
    idle_timeout: str | None
    idle_mode: str | None
    activity_sources: str | None
    worktree_base_folder: str | None
    start_on_boot: bool
    start_host: bool
    extra_provision_command: tuple[str, ...]
    upload_file: tuple[str, ...]
    update: bool
    yes: bool
    tmux_width: int | None
    tmux_height: int | None
    tmux_window_size: str | None
