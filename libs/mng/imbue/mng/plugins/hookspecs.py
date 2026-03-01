from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click
import pluggy
from click_option_group import GroupedOption
from click_option_group import OptionGroup
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mng.config.data_types import MngContext
from imbue.mng.config.data_types import ProviderInstanceConfig
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import HostInterface
from imbue.mng.interfaces.host import NewHostOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.interfaces.provider_backend import ProviderBackendInterface
from imbue.mng.primitives import HostName
from imbue.mng.primitives import ProviderInstanceName

hookspec = pluggy.HookspecMarker("mng")


@hookspec
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]] | None:
    """Register a provider backend with mng.

    Plugins should implement this hook to register provider backends along with
    their configuration class.

    Return a tuple of (backend_class, config_class) to register a backend,
    or None if not registering a backend.

    The config_class should be a subclass of ProviderInstanceConfig that defines
    the configuration options for this backend.
    """


@hookspec
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type | None] | None:
    """Register an agent type with mng.

    Types should implement this hook as a static method to register themselves.
    Return a tuple of (agent_type_name, agent_class, config_class) or None.
    - agent_type_name: The string name for this agent type (e.g., "claude", "codex")
    - agent_class: The AgentInterface implementation class (or None to use BaseAgent)
    - config_class: The AgentTypeConfig subclass (or None to use AgentTypeConfig)
    """


# --- Host lifecycle hooks ---


@hookspec
def on_before_host_create(name: HostName, provider_name: ProviderInstanceName) -> None:
    """[experimental] Called before a new host is created.

    This hook fires before provider.create_host() is called during `mng create`
    when a new host is being created. It does not fire when an existing host is reused.

    If a hook raises an exception, host creation is aborted.
    """


@hookspec
def on_host_created(host: HostInterface) -> None:
    """[experimental] Called after a new host has been created.

    This hook fires after provider.create_host() completes during `mng create`
    when a new host was created. It does not fire when an existing host is reused.
    """


@hookspec
def on_before_host_destroy(host: HostInterface) -> None:
    """[experimental] Called before a host is destroyed.

    This hook fires before provider.destroy_host() is called. The host is still
    accessible when this hook runs.

    If a hook raises an exception, host destruction is aborted.
    """


@hookspec
def on_host_destroyed(host: HostInterface) -> None:
    """[experimental] Called after a host has been destroyed.

    This hook fires after provider.destroy_host() completes. The host's
    infrastructure is gone but the Python object is still available for
    reading metadata (name, id, etc.).
    """


# --- Agent lifecycle hooks ---


@hookspec
def on_before_initial_file_copy(agent_options: CreateAgentOptions, host: OnlineHostInterface) -> None:
    """[experimental] Called before copying files to create the agent's work directory.

    This hook fires before host.create_agent_work_dir() is called during `mng create`.
    Only fires when create_work_dir is True.
    """


@hookspec
def on_after_initial_file_copy(
    agent_options: CreateAgentOptions, host: OnlineHostInterface, work_dir_path: Path
) -> None:
    """[experimental] Called after copying files to create the agent's work directory.

    This hook fires after host.create_agent_work_dir() completes during `mng create`.
    Only fires when create_work_dir is True.
    """


@hookspec
def on_agent_state_dir_created(agent: AgentInterface, host: OnlineHostInterface) -> None:
    """[experimental] Called after the agent's state directory has been created.

    This hook fires inside host.create_agent_state(), after the state directory
    and data.json have been written but before provisioning begins.
    """


@hookspec
def on_before_provisioning(agent: AgentInterface, host: OnlineHostInterface) -> None:
    """[experimental] Called before provisioning an agent.

    This hook fires before host.provision_agent() is called during `mng create`.
    """


@hookspec
def on_after_provisioning(agent: AgentInterface, host: OnlineHostInterface) -> None:
    """[experimental] Called after provisioning an agent.

    This hook fires after host.provision_agent() completes during `mng create`.
    """


@hookspec
def on_agent_created(agent: AgentInterface, host: OnlineHostInterface) -> None:
    """[experimental] Called after an agent has been fully created and started.

    This hook fires at the end of create(), after the agent is started.
    Plugins can use this to perform actions like logging, notifications,
    or custom setup.
    """


@hookspec
def on_before_agent_destroy(agent: AgentInterface, host: OnlineHostInterface) -> None:
    """[experimental] Called before an online agent is destroyed.

    This hook fires before host.destroy_agent() is called. The agent is still
    accessible when this hook runs.

    Only fires for online agents. When an offline host is destroyed (which
    implicitly destroys its agents), on_before_host_destroy fires instead.

    If a hook raises an exception, agent destruction is aborted.
    """


@hookspec
def on_agent_destroyed(agent: AgentInterface, host: OnlineHostInterface) -> None:
    """[experimental] Called after an online agent has been destroyed.

    This hook fires after host.destroy_agent() completes. The agent's state
    directory is gone but the Python object is still available for reading
    metadata (name, id, type, etc.).

    Only fires for online agents. When an offline host is destroyed (which
    implicitly destroys its agents), on_host_destroyed fires instead.
    """


class OptionStackItem(FrozenModel):
    """Specification for a CLI option that plugins can register.

    This provides a typed interface for plugins to add custom CLI options
    to mng subcommands. The fields correspond to click.Option parameters.
    """

    param_decls: tuple[str, ...] = Field(description="Option names, e.g. ('--my-option', '-m')")
    type: Any = Field(
        default=str,
        description="The click type for the option value",
    )
    default: Any = Field(
        default=None,
        description="Default value if option not provided",
    )
    help: str | None = Field(
        default=None,
        description="Help text shown in --help output",
    )
    is_flag: bool = Field(
        default=False,
        description="Whether this is a boolean flag (no value needed)",
    )
    multiple: bool = Field(
        default=False,
        description="Whether the option can be specified multiple times",
    )
    required: bool = Field(
        default=False,
        description="Whether the option is required",
    )
    envvar: str | None = Field(
        default=None,
        description="Environment variable to read value from",
    )

    def to_click_option(self, group: OptionGroup | None = None) -> click.Option:
        """Convert this spec to a click.Option instance.

        If a group is provided, returns a GroupedOption that belongs to that group.
        Otherwise returns a regular click.Option.
        """
        option_class: type[click.Option] = GroupedOption if group else click.Option
        group_kwargs: dict[str, Any] = {"group": group} if group else {}

        # For flag options, don't pass type - click handles it internally
        if self.is_flag:
            return option_class(
                self.param_decls,
                default=self.default,
                help=self.help,
                is_flag=True,
                multiple=self.multiple,
                required=self.required,
                envvar=self.envvar,
                **group_kwargs,
            )
        return option_class(
            self.param_decls,
            type=self.type,
            default=self.default,
            help=self.help,
            is_flag=False,
            multiple=self.multiple,
            required=self.required,
            envvar=self.envvar,
            **group_kwargs,
        )


@hookspec
def register_cli_options(command_name: str) -> Mapping[str, list[OptionStackItem]] | None:
    """Register custom CLI options for a mng subcommand.

    Plugins can implement this hook to add custom command-line options
    to mng subcommands. This is similar to pytest's pytest_addoption hook.

    Return a mapping of group_name -> list[OptionStackItem], or None if no options
    are being added. If the group already exists on the command, new options will
    be merged into it. If the group is new, a new option group will be created.
    """


@hookspec
def on_load_config(config_dict: dict[str, Any]) -> None:
    """Called when loading configuration, before final validation.

    This hook is called right before MngConfig.model_validate() is called,
    allowing plugins to dynamically modify the configuration dictionary.

    The config_dict is passed by reference, so plugins can modify it in place.
    Any changes made will be reflected in the final config object.

    Use cases:
    - Dynamically set configuration values based on environment
    - Inject plugin-specific defaults
    - Transform or normalize configuration values
    """


@hookspec
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register custom CLI commands with mng.

    Plugins can implement this hook to add new top-level commands to mng.
    Return a sequence of click.Command objects to register, or None if not
    registering any commands.

    Each command will be added to the main mng CLI group and will be available
    as `mng <command_name>`. The command's name attribute determines the
    subcommand name.

    Example plugin implementation::

        @hookimpl
        def register_cli_commands() -> Sequence[click.Command] | None:
            return [my_custom_command]

        @click.command()
        @click.option("--example", help="An example option")
        def my_custom_command(example: str) -> None:
            logger.info("Running custom command with: {}", example)
    """


@hookspec
def override_command_options(
    command_name: str,
    command_class: type,
    params: dict[str, Any],
) -> None:
    """Override or modify command options right before the options object is created.

    This hook is called after CLI argument parsing and config defaults have been
    applied, but before the final command options object is instantiated. Plugins
    can use this to mutate or override any command parameter values.

    The params dict contains all parameters that will be passed to the command
    options class constructor. Plugins should modify this dict in place.

    The command_class is provided so plugins can optionally validate their changes
    by attempting to construct the options object (e.g., command_class(**params)).

    Multiple plugins can implement this hook. They are called in registration
    order, and each plugin receives the params as modified by previous plugins.

    Example plugin implementation::

        @hookimpl
        def override_command_options(
            command_name: str,
            command_class: type,
            params: dict[str, Any],
        ) -> None:
            if command_name == "create" and params.get("agent_type") == "claude":
                # Override the model for claude agents
                params["model"] = "opus"
    """


@hookspec
def get_files_for_deploy(
    mng_ctx: MngContext,
    include_user_settings: bool,
    include_project_settings: bool,
    repo_root: Path,
) -> dict[Path, Path | str]:
    """[experimental] Return files to include when deploying scheduled commands.

    Called during schedule deployment to collect files that should be baked
    into the deployment image. Each plugin can contribute files needed for
    its operation in the remote environment.

    Plugins should respect the include_user_settings and include_project_settings
    flags to allow users to control which files are included. When
    include_user_settings is False, plugins should skip files from the user's
    home directory (paths starting with "~"). When include_project_settings is
    False, plugins should skip unversioned project-specific files.

    When resolving project-relative paths, implementations must use repo_root
    as the base directory rather than the current working directory. This
    ensures correct behavior regardless of where the deploy command is invoked.

    Return a dict mapping destination paths to sources (empty dict if none):
    - Keys are destination Paths on the remote machine. Paths starting
      with "~" are placed relative to the user's home directory
      (e.g. Path("~/.claude.json")). Relative paths (without "~" prefix)
      are placed relative to the project working directory (the Dockerfile
      WORKDIR). Absolute paths are not allowed.
    - Values are either a Path to a local file (whose contents will be
      copied) or a str containing the file contents directly.
    """
    return {}


@hookspec
def modify_env_vars_for_deploy(
    mng_ctx: MngContext,
    env_vars: dict[str, str],
) -> None:
    """[experimental] Mutate the env vars dict for scheduled command deployment.

    Called during schedule deployment after the initial environment variables
    have been assembled from --pass-env and --env-file sources. Each plugin
    can add, update, or remove environment variables needed for its operation
    in the remote environment.

    Plugins mutate env_vars in place: set keys to add or update variables,
    delete keys (via pop/del) to remove them. Plugins are called in
    registration order, so later plugins see changes made by earlier ones.
    """


class OnBeforeCreateArgs(FrozenModel):
    """Arguments passed to and returned from the on_before_create hook.

    This bundles all the modifiable arguments to the create() API function.
    Plugins can return a modified copy of this object to change the create behavior.

    Note: source_host is not included because it represents the resolved source
    location which should not typically be modified by plugins. The source_path
    within the resolved location can still be modified if needed via the path field.
    """

    model_config = {"arbitrary_types_allowed": True}

    target_host: OnlineHostInterface | NewHostOptions = Field(
        description="The target host (or options to create one) for the agent"
    )
    agent_options: CreateAgentOptions = Field(description="Options for creating the agent")
    create_work_dir: bool = Field(description="Whether to create a work directory")


@hookspec
def on_before_create(args: OnBeforeCreateArgs) -> OnBeforeCreateArgs | None:
    """Called at the start of create(), before any work is done.

    This hook allows plugins to inspect and modify the arguments that will be
    used to create an agent. Plugins can modify agent_options, target_host,
    source_path, or create_work_dir by returning a modified OnBeforeCreateArgs.

    Hooks are called in a chain: each hook receives the args as modified by
    previous hooks. Return a modified OnBeforeCreateArgs to change values,
    or return None to pass through unchanged.

    Example plugin implementation::

        @hookimpl
        def on_before_create(args: OnBeforeCreateArgs) -> OnBeforeCreateArgs | None:
            if args.agent_options.agent_type == "claude":
                # Override agent name for claude agents
                new_options = args.agent_options.model_copy_update(
                    to_update(args.agent_options.field_ref().name, f"claude-{args.agent_options.name}"),
                )
                return args.model_copy_update(
                    to_update(args.field_ref().agent_options, new_options),
                )
            return None
    """


# --- Program lifecycle hooks ---


@hookspec
def on_post_install(plugin_name: str) -> None:
    """[future] Called after a plugin is installed or upgraded."""


@hookspec
def on_startup() -> None:
    """[experimental] Called when mng starts up, before any command runs."""


@hookspec
def on_before_command(command_name: str, command_params: dict[str, Any]) -> None:
    """[experimental] Called before any command executes.

    Receives the command name and a dict of the resolved command parameters.
    Plugins can raise an exception to abort execution.
    """


@hookspec
def on_after_command(command_name: str, command_params: dict[str, Any]) -> None:
    """[experimental] Called after a command completes successfully."""


@hookspec
def on_error(command_name: str, command_params: dict[str, Any], error: BaseException) -> None:
    """[experimental] Called when a command raises an exception."""


@hookspec
def on_shutdown() -> None:
    """[experimental] Called when mng is shutting down, after the command has completed."""
