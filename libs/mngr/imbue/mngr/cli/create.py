import os
import shlex
import sys
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.api.agent_addr import AgentAddress
from imbue.mngr.api.agent_addr import parse_agent_address
from imbue.mngr.api.connect import connect_to_agent
from imbue.mngr.api.connect import resolve_connect_command
from imbue.mngr.api.connect import run_connect_command
from imbue.mngr.api.create import create as api_create
from imbue.mngr.api.data_types import ConnectionOptions
from imbue.mngr.api.data_types import CreateAgentResult
from imbue.mngr.api.data_types import SourceLocation
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import ResolvedSource
from imbue.mngr.api.find import ensure_agent_started
from imbue.mngr.api.find import ensure_host_started
from imbue.mngr.api.find import get_host_from_list_by_id
from imbue.mngr.api.find import resolve_source_location
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.env_utils import resolve_env_vars
from imbue.mngr.cli.env_utils import resolve_labels
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import emit_final_json
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import CreateCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import AgentNotFoundError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import HostLocation
from imbue.mngr.hosts.host import get_agent_state_dir_path
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import HostLifecycleOptions
from imbue.mngr.interfaces.host import AgentDataOptions
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import AgentGitOptions
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.interfaces.host import AgentLifecycleOptions
from imbue.mngr.interfaces.host import AgentPermissionsOptions
from imbue.mngr.interfaces.host import AgentProvisioningOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import FileModificationSpec
from imbue.mngr.interfaces.host import HostEnvironmentOptions
from imbue.mngr.interfaces.host import NamedCommand
from imbue.mngr.interfaces.host import NewHostBuildOptions
from imbue.mngr.interfaces.host import NewHostOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.host import UploadFileSpec
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentNameStyle
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostNameStyle
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import LogLevel
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import Permission
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import TransferMode
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.utils.duration import parse_duration_to_seconds
from imbue.mngr.utils.editor import EditorSession
from imbue.mngr.utils.git_utils import find_git_worktree_root
from imbue.mngr.utils.git_utils import get_current_git_branch
from imbue.mngr.utils.git_utils import parse_project_name_from_url
from imbue.mngr.utils.logging import LoggingConfig
from imbue.mngr.utils.logging import LoggingSuppressor
from imbue.mngr.utils.name_generator import generate_agent_name

_DEFAULT_NEW_BRANCH_PATTERN: Final[str] = "mngr/*"
_RECOVERED_MESSAGE_FILENAME: Final[str] = "recovered-message.txt"


class _CachedAgentHostLoader(MutableModel):
    """Lazy loader that caches agents grouped by host on first access."""

    mngr_ctx: MngrContext = Field(frozen=True, description="Manager context for loading agents")
    cached_result: dict[DiscoveredHost, list[DiscoveredAgent]] | None = Field(
        default=None, description="Cached loading result"
    )

    def __call__(self) -> dict[DiscoveredHost, list[DiscoveredAgent]]:
        if self.cached_result is None:
            self.cached_result = discover_hosts_and_agents(
                self.mngr_ctx,
                provider_names=None,
                agent_identifiers=None,
                include_destroyed=False,
                reset_caches=False,
            )[0]
        return self.cached_result


@pure
def _is_new_host_implied(address: AgentAddress) -> bool:
    """True when the address implies creating a new host (NAME@.PROVIDER form)."""
    return address.provider_name is not None and address.host_name is None


@pure
def _is_creating_new_host(address: AgentAddress, new_host_flag: bool) -> bool:
    """Whether this address combined with the --new-host flag means creating a new host."""
    return new_host_flag or _is_new_host_implied(address)


@pure
def _make_name_style_choices() -> list[str]:
    """Get lowercase name style choices."""
    return [s.value.lower() for s in AgentNameStyle]


@pure
def _make_host_name_style_choices() -> list[str]:
    """Get lowercase host name style choices."""
    return [s.value.lower() for s in HostNameStyle]


@pure
def _make_log_level_choices() -> list[str]:
    """Get log level choices."""
    return [level.value for level in LogLevel]


@pure
def _make_idle_mode_choices() -> list[str]:
    """Get lowercase idle mode choices."""
    return [m.value.lower() for m in IdleMode]


@pure
def _make_output_format_choices() -> list[str]:
    """Get lowercase output format choices."""
    return [f.value.lower() for f in OutputFormat]


class _CreateCommand(click.Command):
    """Custom Command subclass that correctly handles -- for agent arg passthrough.

    Click's default behavior fills unfilled optional positional arguments from
    args after -- before putting the rest into the variadic. For example, in
    ``mngr create selene --type claude -- --dangerously-skip-permissions``,
    Click would assign ``--dangerously-skip-permissions`` to
    ``positional_agent_type`` instead of ``agent_args``.

    This override strips everything after -- before Click's parser runs, then
    appends the stripped args to ``agent_args`` after parsing completes.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if "--" in args:
            idx = args.index("--")
            after_dash = tuple(args[idx + 1 :])
            args = args[:idx]
        else:
            after_dash = ()
        result = super().parse_args(ctx, args)
        ctx.params["agent_args"] = ctx.params.get("agent_args", ()) + after_dash
        return result


@click.command(cls=_CreateCommand)
@click.argument("positional_name", default=None, required=False)
@click.argument("positional_agent_type", default=None, required=False)
@click.argument("agent_args", nargs=-1, type=click.UNPROCESSED)
@optgroup.group("Agent Options")
@optgroup.option(
    "-t",
    "--template",
    multiple=True,
    help="Use a named template from create_templates config [repeatable, stacks in order]",
)
@optgroup.option(
    "-n",
    "--name",
    help="Agent address (alternative to positional argument, mutually exclusive) [default: auto-generated]",
)
@optgroup.option("--id", help="Explicit agent ID [default: auto-generated]")
@optgroup.option(
    "--name-style",
    type=click.Choice(_make_name_style_choices(), case_sensitive=False),
    default="coolname",
    show_default=True,
    help="Auto-generated name style",
)
@optgroup.option("--type", help="Which type of agent to run [default: claude]")
@optgroup.option(
    "--command",
    help="Run a literal command using the generic agent type (mutually exclusive with --type)",
)
# FOLLOWUP: hmm... I wonder if the name of this should be changed to something more like "window" to be more closely aligned with the tmux primitive it actually creates...
#  more generally, we probably need to do a pass at refining *all* of these option names...
@optgroup.option(
    "-w",
    "--extra-window",
    multiple=True,
    help='Run extra command in additional window. Use name="command" to set window name. Note: ALL_UPPERCASE names (e.g., FOO="bar") are treated as env var assignments, not window names',
)
@optgroup.option("--label", multiple=True, help="Agent label KEY=VALUE [repeatable] [experimental]")
@optgroup.group("Host Options")
@optgroup.option(
    "--provider",
    help="Provider for the host (alternative to .PROVIDER in the address, e.g. --provider docker)",
)
@optgroup.option(
    "--new-host",
    is_flag=True,
    default=False,
    help="Force creating a new host (requires a provider via address or --provider)",
)
@optgroup.option(
    "--project",
    help="Project name for the agent (sets the 'project' label) [default: derived from git remote origin or folder name]",
)
@optgroup.option("--host-label", multiple=True, help="Host metadata label KEY=VALUE [repeatable]")
@optgroup.option(
    "--host-name-style",
    type=click.Choice(_make_host_name_style_choices(), case_sensitive=False),
    default="coolname",
    show_default=True,
    help="Auto-generated host name style",
)
@optgroup.group("Behavior")
@optgroup.option(
    "--reuse/--no-reuse",
    default=False,
    show_default=True,
    help="Reuse existing agent with the same name if it exists (idempotent create)",
)
@optgroup.option("--connect/--no-connect", default=True, help="Connect to the agent after creation [default: connect]")
@optgroup.option(
    "--auto-start/--no-auto-start",
    "start_host",
    default=True,
    show_default=True,
    help="Automatically start offline hosts (source and target) before proceeding",
)
@optgroup.group("Agent Source Data (what to include in the new agent)")
@optgroup.option(
    "--from",
    "--source",
    "source",
    help="Directory to use as work_dir root [AGENT | AGENT@HOST | AGENT@HOST.PROVIDER:PATH | @HOST:PATH]. Defaults to current dir if no other source args are given",
)
@optgroup.option("--source-agent", "--from-agent", "source_agent", help="Source agent for cloning work_dir")
@optgroup.option("--source-host", help="Source host")
@optgroup.option("--source-path", help="Source path")
@optgroup.option(
    "--rsync/--no-rsync",
    default=None,
    help="Use rsync for file transfer [default: yes if rsync-args are present or if git is disabled]",
)
@optgroup.option("--rsync-args", help="Additional arguments to pass to rsync")
@optgroup.option("--include-git/--no-include-git", default=True, show_default=True, help="Include .git directory")
@optgroup.group("Agent Target (where to put the new agent)")
@optgroup.option("--target", help="Target [HOST][:PATH]. Defaults to current dir if no other target args are given")
@optgroup.option(
    "--target-path", help="Directory to mount source inside agent host. Incompatible with --transfer=none"
)
@optgroup.option(
    "--transfer",
    type=click.Choice(["none", "rsync", "git-mirror", "git-worktree"], case_sensitive=False),
    default=None,
    help="How to transfer the project into the agent. "
    "none: run in-place (no transfer). "
    "rsync: copy via rsync (non-git projects). "
    "git-mirror: transfer via git push --mirror (git projects). "
    "git-worktree: create a git worktree (git projects, local only). "
    "[default: git-worktree for local git repos, git-mirror for remote git repos, rsync for non-git]",
)
@optgroup.group("Agent Git Configuration")
@optgroup.option(
    "--branch",
    default=f":{_DEFAULT_NEW_BRANCH_PATTERN}",
    show_default=True,
    help="Branch spec as [BASE][:NEW]. "
    "BASE defaults to current branch. "
    "NEW creates a fresh branch (* is replaced by agent name). "
    "Omit :NEW to use BASE directly without creating a branch. "
    f"Empty NEW (e.g. 'main:') defaults to {_DEFAULT_NEW_BRANCH_PATTERN}.",
)
@optgroup.option("--depth", type=int, help="Shallow clone depth [default: full]")
@optgroup.option("--shallow-since", help="Shallow clone since date")
@optgroup.option(
    "--ensure-clean/--no-ensure-clean", default=True, show_default=True, help="Abort if working tree is dirty"
)
@optgroup.option(
    "--include-unclean/--exclude-unclean",
    "include_unclean",
    default=None,
    help="Include uncommitted files [default: include if --no-ensure-clean]",
)
@optgroup.option(
    "--include-gitignored/--no-include-gitignored",
    default=False,
    show_default=True,
    help="Include gitignored files",
)
@optgroup.option(
    "--worktree-base-folder",
    default=None,
    type=click.Path(),
    help="Base folder for git worktrees [default: ~/.mngr/worktrees/]",
)
@optgroup.group("Agent Environment Variables")
@optgroup.option("--env", multiple=True, help="Set environment variable KEY=VALUE")
@optgroup.option(
    "--env-file",
    type=click.Path(exists=True),
    multiple=True,
    help="Load env",
)
@optgroup.option("--pass-env", multiple=True, help="Forward variable from shell")
@optgroup.group("Agent Provisioning")
@optgroup.option("--grant", "grant", multiple=True, help="Grant a permission to the agent [repeatable]")
@optgroup.option(
    "--extra-provision-command",
    "extra_provision_command",
    multiple=True,
    help="Run custom shell command during provisioning [repeatable]",
)
@optgroup.option("--upload-file", "upload_file", multiple=True, help="Upload LOCAL:REMOTE file pair [repeatable]")
@optgroup.option("--append-to-file", "append_to_file", multiple=True, help="Append REMOTE:TEXT to file [repeatable]")
@optgroup.option(
    "--prepend-to-file", "prepend_to_file", multiple=True, help="Prepend REMOTE:TEXT to file [repeatable]"
)
@optgroup.group("New Host Environment Variables")
@optgroup.option("--host-env", multiple=True, help="Set environment variable KEY=VALUE for host [repeatable]")
@optgroup.option(
    "--host-env-file", type=click.Path(exists=True), multiple=True, help="Load env file for host [repeatable]"
)
@optgroup.option("--pass-host-env", multiple=True, help="Forward variable from shell for host [repeatable]")
@optgroup.group("New Host Build")
@optgroup.option("--snapshot", help="Use existing snapshot instead of building")
@optgroup.option(
    "-b",
    "--build-arg",
    multiple=True,
    help="Build argument as key=value or --key=value (e.g., -b gpu=h100 -b cpu=2) [repeatable]",
)
@optgroup.option("-s", "--start-arg", multiple=True, help="Argument for start [repeatable]")
@optgroup.group("Host Lifecycle")
@optgroup.option(
    "--idle-timeout",
    type=str,
    help="Shutdown after idle for specified duration (e.g., 30s, 5m, 1h, or plain seconds) [default: none]",
)
@optgroup.option(
    "--idle-mode",
    type=click.Choice(_make_idle_mode_choices(), case_sensitive=False),
    help="When to consider host idle [default: io if remote, disabled if local]",
)
@optgroup.option("--activity-sources", help="Activity sources for idle detection (comma-separated)")
@optgroup.option(
    "--start-on-boot/--no-start-on-boot", "start_on_boot", default=None, help="Restart on host boot [default: no]"
)
@optgroup.group("Connection Options")
@optgroup.option(
    "--reconnect/--no-reconnect", default=True, show_default=True, help="Automatically reconnect if dropped"
)
@optgroup.option(
    "--interactive/--no-interactive",
    "interactive",
    default=None,
    help="Enable interactive mode [default: yes if TTY]",
)
@optgroup.option("--message", help="Initial message to send after the agent starts")
@optgroup.option("--message-file", type=click.Path(exists=True), help="File containing initial message to send")
@optgroup.option(
    "--edit-message",
    is_flag=True,
    help="Open an editor to compose the initial message (uses $EDITOR). Editor runs in parallel with agent creation. If --message or --message-file is provided, their content is used as initial editor content.",
)
@optgroup.option("--retry", type=int, default=3, show_default=True, help="Number of connection retries")
@optgroup.option("--retry-delay", default="5s", show_default=True, help="Delay between retries (e.g., 5s, 1m)")
@optgroup.option("--attach-command", help="Command to run instead of attaching to main session")
@optgroup.option(
    "--connect-command",
    help="Command to run instead of the builtin connect. MNGR_AGENT_NAME and MNGR_SESSION_NAME env vars are set.",
)
@optgroup.group("Automation")
@optgroup.option(
    "-y",
    "--yes",
    is_flag=True,
    default=False,
    help="Auto-approve all prompts (e.g., skill installation) without asking",
)
@add_common_options
@click.pass_context
def create(ctx: click.Context, **kwargs) -> None:
    # Setup command context (config, logging, output options)
    # This loads the config, applies defaults, and creates the final options
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="create",
        command_class=CreateCliOptions,
    )
    logging_config: LoggingConfig = ctx.meta["logging_config"]

    # Parse agent address from the positional argument or --name flag.
    # Both accept agent addresses; they are equivalent but mutually exclusive.
    if opts.positional_name and opts.name:
        raise UserInputError("Cannot specify both a positional agent address and --name. Use one or the other.")
    address = parse_agent_address(opts.positional_name or opts.name or "")

    # Merge --provider flag into the address (alternative to .PROVIDER in the address).
    if opts.provider:
        flag_provider = ProviderInstanceName(opts.provider)
        if address.provider_name is not None and address.provider_name != flag_provider:
            raise UserInputError(
                f"Conflicting providers: address has '{address.provider_name}' "
                f"but --provider is '{flag_provider}'. Use one or the other."
            )
        if address.provider_name is None:
            address = address.model_copy_update(
                to_update(address.field_ref().provider_name, flag_provider),
            )

    # Apply --yes flag to auto-approve prompts (e.g., skill installation)
    if opts.yes:
        mngr_ctx = mngr_ctx.model_copy_update(
            to_update(mngr_ctx.field_ref().is_auto_approve, True),
        )

    # Collect plugin-registered CLI params so they can be merged into plugin_data.
    # Filter None (unset single options) and empty tuples (unset multiple options).
    plugin_cli_params: dict[str, Any] = {
        k: v for k, v in ctx.meta.get("plugin_cli_params", {}).items() if v is not None and v != ()
    }

    # Setup (validation, editor session, source resolution, etc.)
    setup = _setup_create(mngr_ctx, output_opts, opts, logging_config, address, plugin_cli_params)

    # Create agent
    create_result, connection_opts = _create_agent(mngr_ctx, output_opts, opts, setup)
    _post_create(create_result, connection_opts, opts, mngr_ctx)
    _finish_create(create_result, setup, output_opts)


class _AutoLabels(FrozenModel):
    """Auto-derived agent labels. Field names are the label keys."""

    project: str = Field(description="Project name (from git remote or folder name)")
    remote: str | None = Field(
        default=None,
        description="Git remote origin URL (stored verbatim, may include credentials if the remote uses HTTPS with an embedded PAT)",
    )


class _CreateSetup(FrozenModel):
    """Per-invocation state shared between _setup_create and _create_agent."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    address: AgentAddress = Field(description="Parsed agent address from the positional argument")
    initial_message: str | None = Field(
        description="Resolved initial message content (from --message or --message-file)"
    )
    editor_session: EditorSession | None = Field(default=None, description="Editor session for --edit-message")
    agent_and_host_loader: _CachedAgentHostLoader = Field(description="Lazy loader for agents grouped by host")
    resolved_source: ResolvedSource = Field(description="Resolved source location and optional source agent")
    auto_labels: _AutoLabels = Field(description="Auto-derived labels for the new agent")
    host_lifecycle: HostLifecycleOptions = Field(description="Host lifecycle options")
    plugin_cli_params: dict[str, Any] = Field(
        default_factory=dict, description="Plugin-registered CLI params to merge into plugin_data"
    )


def _setup_create(
    mngr_ctx: MngrContext,
    output_opts: OutputOptions,
    opts: CreateCliOptions,
    logging_config: LoggingConfig,
    address: AgentAddress,
    plugin_cli_params: dict[str, Any] | None = None,
) -> _CreateSetup:
    """Validate options, resolve messages, start editor session, resolve source location."""
    # Validate that both --message and --message-file are not provided
    if opts.message is not None and opts.message_file is not None:
        raise UserInputError("Cannot provide both --message and --message-file")

    # Read message from file if --message-file is provided (used as initial content for editor if --edit-message)
    initial_message_content: str | None
    if opts.message_file is not None:
        message_file_path = Path(opts.message_file)
        initial_message_content = message_file_path.read_text()
    elif opts.message is not None:
        initial_message_content = opts.message
    else:
        initial_message_content = None

    # If --edit-message is set, start the editor immediately
    # The editor runs in parallel with agent creation
    # We suppress logging while the editor is open to avoid writing to the terminal
    editor_session: EditorSession | None = None
    if opts.edit_message:
        editor_session = EditorSession.create(initial_content=initial_message_content)
        # Enable logging suppression before starting the editor so that
        # log messages don't interfere with the editor's terminal output
        LoggingSuppressor.enable(logging_config.console_level)
        # Start editor with callback that restores logging when it exits
        editor_session.start(on_exit=_on_editor_exit)
        # When using editor, don't pass message to api_create (we'll send it after editor finishes)
        initial_message = None
    else:
        initial_message = initial_message_content

    # Create a lazy loader for agents grouped by host (only loads if needed)
    agent_and_host_loader = _CachedAgentHostLoader(mngr_ctx=mngr_ctx)

    # figure out where the source data is coming from
    resolved_source = _resolve_source_location(opts, agent_and_host_loader, mngr_ctx, is_start_desired=opts.start_host)

    # derive auto-labels from the source location
    remote_url = _get_source_remote_url(resolved_source.location)
    auto_labels = _AutoLabels(
        project=_parse_project_name(resolved_source, opts, remote_url),
        remote=remote_url,
    )

    # Parse host lifecycle options (these go on the host, not the agent)
    host_lifecycle = _parse_host_lifecycle_options(opts)

    return _CreateSetup(
        address=address,
        initial_message=initial_message,
        editor_session=editor_session,
        agent_and_host_loader=agent_and_host_loader,
        resolved_source=resolved_source,
        auto_labels=auto_labels,
        host_lifecycle=host_lifecycle,
        plugin_cli_params=plugin_cli_params or {},
    )


def _create_agent(
    mngr_ctx: MngrContext,
    output_opts: OutputOptions,
    opts: CreateCliOptions,
    setup: _CreateSetup,
) -> tuple[CreateAgentResult, ConnectionOptions]:
    """Parse opts, resolve host, create agent."""
    address = setup.address

    # Parse target host (existing or new)
    target_host = _parse_target_host(
        opts=opts,
        address=address,
        agent_and_host_loader=setup.agent_and_host_loader,
        lifecycle=setup.host_lifecycle,
    )

    # Compute source agent state dir from the resolved agent ID
    source_agent_state_dir: Path | None = None
    if setup.resolved_source.agent is not None:
        source_agent_state_dir = get_agent_state_dir_path(
            setup.resolved_source.location.host.host_dir, setup.resolved_source.agent.agent_id
        )

    # Parse agent options
    agent_opts, has_explicit_base = _parse_agent_opts(
        opts=opts,
        address=address,
        initial_message=setup.initial_message,
        source_location=setup.resolved_source.location,
        source_agent_state_dir=source_agent_state_dir,
        mngr_ctx=mngr_ctx,
    )

    # Merge plugin-registered CLI params into plugin_data so plugin hooks can access them
    if setup.plugin_cli_params:
        merged = {**agent_opts.plugin_data, **setup.plugin_cli_params}
        agent_opts = agent_opts.model_copy_update(
            to_update(agent_opts.field_ref().plugin_data, merged),
        )

    # parse the connection options
    connection_opts = ConnectionOptions(
        is_reconnect=opts.reconnect,
        is_interactive=opts.interactive,
        message=None,
        retry_count=opts.retry,
        retry_delay=opts.retry_delay,
        attach_command=opts.attach_command,
    )

    # If --reuse is set, try to find and reuse an existing agent with the same name
    if opts.reuse and agent_opts.name is not None:
        reuse_result = _try_reuse_existing_agent(
            agent_name=agent_opts.name,
            provider_name=address.provider_name,
            target_host_ref=target_host if isinstance(target_host, DiscoveredHost) else None,
            mngr_ctx=mngr_ctx,
            agent_and_host_loader=setup.agent_and_host_loader,
        )
        if reuse_result is not None:
            agent, host = reuse_result
            logger.info("Reusing existing agent: {}", agent.name)

            # Handle --edit-message if editor session was started,
            # or send initial message directly if --message/--message-file was provided
            with _editor_cleanup_scope(setup.editor_session):
                if setup.editor_session is not None:
                    # Hold the host lock while waiting for the editor to prevent
                    # idle shutdown during long editing sessions
                    with host.lock_cooperatively():
                        _handle_editor_message(
                            editor_session=setup.editor_session,
                            agent=agent,
                        )
                elif setup.initial_message is not None:
                    # Send initial message directly (from --message or --message-file)
                    logger.info("Sending message to agent")
                    agent.send_message(setup.initial_message)
                else:
                    pass

            return CreateAgentResult(agent=agent, host=host), connection_opts

    # If ensure-clean is set, verify the source work_dir is clean.
    # Skip the check when using an explicit base branch, since the agent will be
    # created from that branch and uncommitted changes in the current working tree
    # are irrelevant (regardless of transfer mode).
    is_from_explicit_base = agent_opts.git is not None and has_explicit_base
    if opts.ensure_clean and not is_from_explicit_base:
        _ensure_clean_work_dir(setup.resolved_source.location)

    # figure out the target host (if we just have a reference)
    resolved_target_host = _resolve_target_host(target_host, mngr_ctx, is_start_desired=opts.start_host)

    # Set host labels on existing hosts (for new hosts, labels are passed via NewHostOptions).
    # This ensures local hosts get any --host-label values.
    if isinstance(resolved_target_host, OnlineHostInterface):
        _apply_host_labels(resolved_target_host, opts.host_label)

    # Set auto-derived labels (project, remote) on the agent (labels are agent-level, not host-level).
    # User-specified --label values take precedence over auto-derived ones.
    auto_labels = setup.auto_labels.model_dump(exclude_none=True)
    agent_opts = agent_opts.model_copy_update(
        to_update(
            agent_opts.field_ref().label_options,
            AgentLabelOptions(labels={**auto_labels, **agent_opts.label_options.labels}),
        ),
    )

    # Call the API create function
    with _editor_cleanup_scope(setup.editor_session):
        create_result = api_create(
            source_location=setup.resolved_source.location,
            target_host=resolved_target_host,
            agent_options=agent_opts,
            mngr_ctx=mngr_ctx,
        )

        # If --edit-message was used, wait for editor and send the message.
        # Re-acquire the host lock to prevent idle shutdown while the user edits
        # (api_create releases its lock before returning).
        if setup.editor_session is not None:
            with create_result.host.lock_cooperatively():
                _handle_editor_message(
                    editor_session=setup.editor_session,
                    agent=create_result.agent,
                )

    return create_result, connection_opts


def _post_create(
    create_result: CreateAgentResult,
    connection_opts: ConnectionOptions,
    opts: CreateCliOptions,
    mngr_ctx: MngrContext,
) -> None:
    """Post-creation: connect."""
    if opts.connect:
        resolved_connect_command = resolve_connect_command(opts.connect_command, mngr_ctx)
        if resolved_connect_command is not None:
            session_name = f"{mngr_ctx.config.prefix}{create_result.agent.name}"
            run_connect_command(
                resolved_connect_command,
                str(create_result.agent.name),
                session_name,
                is_local=create_result.host.is_local,
            )
        else:
            connect_to_agent(create_result.agent, create_result.host, mngr_ctx, connection_opts)


def _finish_create(
    result: CreateAgentResult,
    setup: _CreateSetup,
    output_opts: OutputOptions,
) -> None:
    """Wrap-up: editor cleanup, output result."""
    # Ensure editor cleanup on all exit paths (may already be cleaned up by _create_agent)
    if setup.editor_session is not None and not setup.editor_session.is_finished():
        setup.editor_session.cleanup()
    if LoggingSuppressor.is_suppressed():
        LoggingSuppressor.disable_and_replay(clear_screen=True)

    _output_result(result, output_opts)


def _on_editor_exit() -> None:
    """Callback invoked when the editor process exits.

    Restores logging by disabling suppression and replaying buffered messages.
    This is called from a background thread as soon as the editor exits.
    """
    LoggingSuppressor.disable_and_replay(clear_screen=True)


@contextmanager
def _editor_cleanup_scope(
    editor_session: EditorSession | None,
    recovery_dir: Path | None = None,
) -> Iterator[None]:
    """Ensure editor session cleanup and logging suppressor restoration on exit.

    On failure, saves any editor content to a recovery file before cleanup so
    the user does not lose their work.

    Safe to nest: EditorSession.cleanup() is idempotent, and
    LoggingSuppressor.disable_and_replay() is a no-op when not suppressed.
    """
    try:
        yield
    finally:
        if editor_session is not None:
            # If exiting due to an exception, rescue the editor content before
            # cleanup deletes the temp file
            if sys.exc_info()[0] is not None:
                _rescue_editor_content(editor_session, recovery_dir=recovery_dir)
            editor_session.cleanup()
        if LoggingSuppressor.is_suppressed():
            LoggingSuppressor.disable_and_replay(clear_screen=True)


def _rescue_editor_content(
    editor_session: EditorSession,
    recovery_dir: Path | None = None,
) -> None:
    """Save editor content to a recovery file so the user does not lose their work.

    Reads the content from the editor's temp file (which still exists before cleanup)
    and writes it to ~/.mngr/recovered-message.txt. Uses logger.warning which will be
    buffered if logging is suppressed and replayed when suppression is disabled.
    """
    if not editor_session.temp_file_path.exists():
        return

    try:
        content = editor_session.temp_file_path.read_text().rstrip()
    except OSError as e:
        logger.trace("Failed to read editor temp file for recovery: {}", e)
        return

    if not content:
        return

    # Save to ~/.mngr/recovered-message.txt
    resolved_recovery_dir = recovery_dir if recovery_dir is not None else Path.home() / ".mngr"
    resolved_recovery_dir.mkdir(parents=True, exist_ok=True)
    recovery_path = resolved_recovery_dir / _RECOVERED_MESSAGE_FILENAME

    try:
        recovery_path.write_text(content)
    except OSError as e:
        logger.trace("Failed to write recovery file {}: {}", recovery_path, e)
        return

    logger.warning("Your editor message has been saved to: {}", recovery_path)


def _handle_editor_message(
    editor_session: EditorSession,
    agent: AgentInterface,
) -> None:
    """Wait for the editor to finish and send the edited message to the agent.

    If the editor exits with a non-zero code, is cancelled, or the content is empty,
    no message is sent and a warning is logged.

    Note: No message delay is applied here because by the time the user finishes
    editing, the agent has been running in parallel and is already ready.

    Logging suppression is disabled automatically by the editor's on_exit callback
    as soon as the editor process exits. By the time wait_for_result() returns,
    the callback has already restored logging.
    """
    with _editor_cleanup_scope(editor_session):
        with log_span("Waiting for editor to finish..."):
            edited_message = editor_session.wait_for_result()

        # By this point, the on_exit callback has already restored logging
        # (it's called as soon as the editor process exits)

        if edited_message is None:
            logger.warning("No message to send (editor was closed without saving or content is empty)")
            return

        logger.info("Sending edited message...")
        agent.send_message(edited_message)
        logger.debug("Message sent successfully")


def _get_source_remote_url(source_location: HostLocation) -> str | None:
    """Get the git remote origin URL from the source location via execute_command.

    Returns the URL verbatim, which may include embedded credentials (e.g. a
    GitHub PAT in an HTTPS URL). This is intentional -- stripping credentials
    would break gh CLI auth for repos that rely on PAT-based HTTPS remotes.
    """
    result = source_location.host.execute_idempotent_command("git remote get-url origin", cwd=source_location.path)
    if result.success and result.stdout.strip():
        return result.stdout.strip()
    return None


def _parse_project_name(
    resolved_source: ResolvedSource,
    opts: CreateCliOptions,
    remote_url: str | None,
) -> str:
    """Determine the project name for a new agent.

    Priority: explicit --project flag > source agent's project label > git remote > folder name.
    """
    if opts.project:
        return opts.project

    # If creating from an existing agent, inherit its project label
    if resolved_source.agent is not None:
        source_project = resolved_source.agent.labels.get("project")
        if source_project is not None:
            return source_project

    # Derive from the already-fetched remote URL (works for both local and remote hosts)
    if remote_url is not None:
        project_name = parse_project_name_from_url(remote_url)
        if project_name is not None:
            return project_name

    # Fall back to the source directory name (resolve to normalize symlinks / '..' components)
    return resolved_source.location.path.resolve().name


def _try_reuse_existing_agent(
    agent_name: AgentName,
    provider_name: ProviderInstanceName | None,
    target_host_ref: DiscoveredHost | None,
    mngr_ctx: MngrContext,
    agent_and_host_loader: Callable[[], dict[DiscoveredHost, list[DiscoveredAgent]]],
) -> tuple[AgentInterface, OnlineHostInterface] | None:
    """Try to find and start an existing agent with the given name.

    Searches for an agent matching the name, scoped by provider and host if specified.
    If found, ensures the agent is started and returns it along with its host.
    If not found, returns None so the caller can proceed with creating a new agent.
    """
    agents_by_host = agent_and_host_loader()

    matching_agents: list[tuple[DiscoveredHost, DiscoveredAgent]] = []

    for host_ref, agent_refs in agents_by_host.items():
        # Skip hosts that don't match the provider filter (if specified)
        if provider_name is not None and host_ref.provider_name != provider_name:
            continue

        # Skip hosts that don't match the target host filter (if specified)
        if target_host_ref is not None and host_ref.host_id != target_host_ref.host_id:
            continue

        for agent_ref in agent_refs:
            if agent_ref.agent_name == agent_name:
                matching_agents.append((host_ref, agent_ref))

    if len(matching_agents) == 0:
        logger.debug("Failed to find existing agent with name: {}", agent_name)
        return None

    if len(matching_agents) > 1:
        raise UserInputError(
            f"Multiple agents found with name '{agent_name}'. Use address syntax (e.g. '{agent_name}@HOST.PROVIDER') to target a specific host."
        )

    host_ref, agent_ref = matching_agents[0]
    logger.debug("Found existing agent {} on host {}", agent_ref.agent_id, host_ref.host_name)

    # Get the provider and host
    provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
    host = provider.get_host(host_ref.host_id)

    # Ensure the host is started
    online_host, _was_started = ensure_host_started(host, is_start_desired=True, provider=provider)

    # Find the agent interface on the online host
    agent: AgentInterface | None = None
    for a in online_host.get_agents():
        if a.id == agent_ref.agent_id:
            agent = a
            break

    if agent is None:
        # Agent not found on online host - this could happen if the host came online
        # but the agent data is stale. Return None to create a new agent.
        logger.warning("Agent {} not found on host after starting, will create new agent", agent_name)
        return None

    # Ensure the agent is started (reusing shared logic from find.py)
    ensure_agent_started(agent, online_host, is_start_desired=True)

    return agent, online_host


def _resolve_source_location(
    opts: CreateCliOptions,
    agent_and_host_loader: Callable[[], dict[DiscoveredHost, list[DiscoveredAgent]]],
    mngr_ctx: MngrContext,
    *,
    is_start_desired: bool,
) -> ResolvedSource:
    """Resolve the source location and optionally the source agent ID and labels."""
    # figure out the agent source data
    if opts.source is None and opts.source_agent is None and opts.source_host is None:
        # easy, source location is on current host
        source_path = opts.source_path
        if source_path is None:
            git_root = find_git_worktree_root(None, mngr_ctx.concurrency_group)
            source_path = str(git_root) if git_root is not None else os.getcwd()
        provider = get_provider_instance(LOCAL_PROVIDER_NAME, mngr_ctx)
        host = provider.get_host(HostName(LOCAL_HOST_NAME))
        online_host, _ = ensure_host_started(host, is_start_desired=is_start_desired, provider=provider)
        return ResolvedSource(location=HostLocation(host=online_host, path=Path(source_path)))

    # Parse the source first to check if it's just a local path.
    # When --source is a plain filesystem path (no agent or host component),
    # we can resolve it locally without loading all providers. Loading all
    # providers is expensive and can fail if a provider's external service
    # (e.g. Docker daemon, Modal credentials) is unavailable.
    parsed = _parse_source_string(opts.source) if opts.source else None
    has_agent_or_host = (
        (parsed is not None and (parsed.agent_name is not None or parsed.host_name is not None))
        or opts.source_agent is not None
        or opts.source_host is not None
    )
    if not has_agent_or_host:
        # Just a local path -- use the fast local-provider path
        if parsed is not None and parsed.path is not None:
            source_path = str(parsed.path)
        elif opts.source_path is not None:
            source_path = opts.source_path
        else:
            source_path = os.getcwd()
        provider = get_provider_instance(LOCAL_PROVIDER_NAME, mngr_ctx)
        host = provider.get_host(HostName(LOCAL_HOST_NAME))
        online_host, _ = ensure_host_started(host, is_start_desired=is_start_desired, provider=provider)
        return ResolvedSource(location=HostLocation(host=online_host, path=Path(source_path)))

    # Need full resolution across providers
    agents_by_host = agent_and_host_loader()
    return resolve_source_location(
        opts.source,
        opts.source_agent,
        opts.source_host,
        opts.source_path,
        agents_by_host,
        mngr_ctx,
        is_start_desired=is_start_desired,
    )


def _resolve_target_host(
    target_host: DiscoveredHost | NewHostOptions | None,
    mngr_ctx: MngrContext,
    *,
    is_start_desired: bool,
) -> OnlineHostInterface | NewHostOptions:
    resolved_target_host: OnlineHostInterface | NewHostOptions
    if target_host is None:
        # No host specified, use the local provider's default host
        provider = get_provider_instance(LOCAL_PROVIDER_NAME, mngr_ctx)
        host = provider.get_host(HostName(LOCAL_HOST_NAME))
        resolved_target_host, _ = ensure_host_started(host, is_start_desired=is_start_desired, provider=provider)
    elif isinstance(target_host, DiscoveredHost):
        provider = get_provider_instance(target_host.provider_name, mngr_ctx)
        host = provider.get_host(target_host.host_id)
        resolved_target_host, _ = ensure_host_started(host, is_start_desired=is_start_desired, provider=provider)
    else:
        resolved_target_host = target_host
    return resolved_target_host


def _find_source_location(
    source: str | None, source_agent: str | None, source_host: str | None, source_path: str | None
) -> SourceLocation:
    # Assemble source - parse the unified --source string if provided
    parsed_source_path: Path | None = None
    parsed_source_agent = source_agent
    parsed_source_host = source_host

    if source:
        # Parse [AGENT[@HOST[.PROVIDER]]]:PATH format
        parsed = _parse_source_string(source)
        parsed_source_path = parsed.path
        parsed_source_agent = parsed.agent_name
        parsed_source_host = parsed.host_name

    # Override with explicit options if provided
    if source_path:
        parsed_source_path = Path(source_path)

    # Build location first so we can use it for validation
    source_location = SourceLocation(
        path=parsed_source_path,
        agent_name=AgentName(parsed_source_agent) if parsed_source_agent else None,
        host_name=HostName(parsed_source_host) if parsed_source_host else None,
    )
    return source_location


def _get_current_git_branch(source_location: HostLocation, mngr_ctx: MngrContext) -> str | None:
    if not source_location.host.is_local:
        raise NotImplementedError(
            "Have to re-implement this function so that it works via HostInterface calls instead!"
        )

    return get_current_git_branch(source_location.path, mngr_ctx.concurrency_group)


def _is_git_repo(path: Path, cg: ConcurrencyGroup) -> bool:
    """Check if the given path is inside a git repository."""
    return find_git_worktree_root(path, cg) is not None


@pure
def _split_cli_args(args: tuple[str, ...]) -> list[str]:
    """Shell-tokenize each CLI arg and flatten into a single list.

    Handles cases like -b "--cpu 16" where the shell passes "--cpu 16" as a
    single string that needs to be split into ["--cpu", "16"].
    """
    return [token for arg in args for token in shlex.split(arg)]


_TRANSFER_MODE_FROM_CLI: dict[str, TransferMode] = {
    "none": TransferMode.NONE,
    "rsync": TransferMode.RSYNC,
    "git-mirror": TransferMode.GIT_MIRROR,
    "git-worktree": TransferMode.GIT_WORKTREE,
}


def _resolve_transfer_mode(
    opts: CreateCliOptions,
    address: AgentAddress,
    source_location: HostLocation,
    mngr_ctx: MngrContext,
) -> TransferMode:
    """Resolve the transfer mode from CLI flags and context.

    Validates the combination of transfer mode, project type (git vs non-git),
    and target locality (local vs remote).
    """
    is_git_repo = (
        _is_git_repo(source_location.path, mngr_ctx.concurrency_group) if source_location.host.is_local else True
    )
    is_creating_new_host = _is_creating_new_host(address, opts.new_host)
    is_remote = (
        is_creating_new_host
        and address.provider_name is not None
        and address.provider_name.lower() != LOCAL_PROVIDER_NAME
    ) or not source_location.host.is_local

    # Check if target path points to the same location as source
    is_same_path = False
    if opts.target_path is not None:
        target_resolved = Path(opts.target_path).resolve()
        source_resolved = source_location.path.resolve()
        if target_resolved == source_resolved and not is_remote:
            is_same_path = True

    if opts.transfer is not None:
        # Explicit --transfer flag
        transfer_mode = _TRANSFER_MODE_FROM_CLI[opts.transfer.lower()]
    elif is_same_path:
        # Target path is the same as source path: must be none
        transfer_mode = TransferMode.NONE
    elif is_git_repo and not is_remote:
        transfer_mode = TransferMode.GIT_WORKTREE
    elif is_git_repo and is_remote:
        transfer_mode = TransferMode.GIT_MIRROR
    else:
        # Non-git project: use rsync (generates a target directory if needed)
        transfer_mode = TransferMode.RSYNC

    # Validate the transfer mode against the context
    if is_same_path and transfer_mode != TransferMode.NONE:
        raise UserInputError(
            f"--transfer={opts.transfer} is not compatible with --target-path pointing to the source directory. "
            f"Use --transfer=none or omit --target-path."
        )

    if is_git_repo and transfer_mode == TransferMode.RSYNC:
        raise UserInputError(
            "--transfer=rsync is not supported for git repositories. "
            "Use --transfer=git-mirror, --transfer=git-worktree, or --transfer=none."
        )

    if not is_git_repo and transfer_mode in (TransferMode.GIT_MIRROR, TransferMode.GIT_WORKTREE):
        raise UserInputError(
            f"--transfer={opts.transfer} requires a git repository, but the source is not a git repo. "
            f"Use --transfer=rsync or --transfer=none."
        )

    if is_remote and transfer_mode == TransferMode.GIT_WORKTREE:
        raise UserInputError(
            "--transfer=git-worktree only works for local agents. Use --transfer=git-mirror for remote agents."
        )

    if transfer_mode == TransferMode.NONE and opts.target_path is not None and not is_same_path:
        raise UserInputError(
            "--transfer=none is incompatible with --target-path pointing to a different directory. "
            "Use a different --transfer mode, or omit --target-path."
        )

    return transfer_mode


def _parse_agent_opts(
    opts: CreateCliOptions,
    address: AgentAddress,
    initial_message: str | None,
    source_location: HostLocation,
    mngr_ctx: MngrContext,
    source_agent_state_dir: Path | None = None,
) -> tuple[CreateAgentOptions, bool]:
    # Get agent name from address (which incorporates both positional and --name),
    # otherwise auto-generate
    parsed_agent_name: AgentName
    if address.agent_name is not None:
        parsed_agent_name = address.agent_name
    else:
        parsed_name_style = AgentNameStyle(opts.name_style.upper())
        parsed_agent_name = generate_agent_name(parsed_name_style)

    # Determine transfer mode
    transfer_mode = _resolve_transfer_mode(opts, address, source_location, mngr_ctx)

    # Parse --branch flag: [BASE_BRANCH][:NEW_BRANCH]
    base_branch, new_branch_name, has_explicit_base = _parse_branch_flag(opts.branch, parsed_agent_name)

    # Worktree mode supports both:
    #   --branch foo       -> check out existing branch 'foo' in the worktree
    #   --branch foo:bar   -> create new branch 'bar' from 'foo' in the worktree

    # if the user didn't specify whether to include unclean, then infer from ensure_clean
    if opts.include_unclean is None:
        is_include_unclean = False if opts.ensure_clean else True
    else:
        is_include_unclean = opts.include_unclean

    # Build git options (None if transfer_mode is NONE or RSYNC -- no git involved)
    git: AgentGitOptions | None
    if transfer_mode in (TransferMode.NONE, TransferMode.RSYNC):
        git = None
    else:
        git = AgentGitOptions(
            base_branch=base_branch or _get_current_git_branch(source_location, mngr_ctx),
            new_branch_name=new_branch_name,
            depth=opts.depth,
            shallow_since=opts.shallow_since,
            is_git_synced=opts.include_git,
            is_include_unclean=is_include_unclean,
            is_include_gitignored=opts.include_gitignored,
        )

    # parse source data options
    data_options = AgentDataOptions(
        is_rsync_enabled=bool(
            opts.rsync or opts.rsync_args or transfer_mode in (TransferMode.NONE, TransferMode.RSYNC)
        ),
        rsync_args=opts.rsync_args or "",
    )

    # Parse environment options
    env_vars = resolve_env_vars(opts.pass_env, opts.env)
    env_files = tuple(Path(f) for f in opts.env_file)

    environment = AgentEnvironmentOptions(
        env_vars=env_vars,
        env_files=env_files,
    )

    # Parse agent lifecycle options
    lifecycle = AgentLifecycleOptions(
        is_start_on_boot=opts.start_on_boot,
    )

    # Parse permissions options
    permissions = AgentPermissionsOptions(
        granted_permissions=tuple(Permission(p) for p in opts.grant),
    )

    # Parse label options
    label_options = resolve_labels(opts.label)

    # Parse provisioning options
    provisioning = AgentProvisioningOptions(
        extra_provision_commands=opts.extra_provision_command,
        upload_files=tuple(UploadFileSpec.from_string(f) for f in opts.upload_file),
        append_to_files=tuple(FileModificationSpec.from_string(f) for f in opts.append_to_file),
        prepend_to_files=tuple(FileModificationSpec.from_string(f) for f in opts.prepend_to_file),
    )

    # Parse target_path if provided
    parsed_target_path = Path(opts.target_path) if opts.target_path else None

    # Determine agent type: --type and positional are equivalent; specifying both
    # with different values is an error. _CreateCommand.parse_args handles --
    # correctly so positional_agent_type is always a real positional.
    #
    # Special case: --command implies using the "generic" agent type, which simply
    # runs the provided command. If --type is also specified to something other
    # than "generic", that's an error (they are mutually exclusive).
    resolved_agent_type = opts.type
    resolved_agent_args = opts.agent_args

    if opts.positional_agent_type and resolved_agent_type and resolved_agent_type != opts.positional_agent_type:
        raise UserInputError(
            f"Conflicting agent types: positional argument says '{opts.positional_agent_type}' "
            f"but --type says '{resolved_agent_type}'. Use one or the other."
        )
    if opts.positional_agent_type and resolved_agent_type is None:
        resolved_agent_type = opts.positional_agent_type

    # Handle --command: it implies using the "generic" agent type
    if opts.command:
        if resolved_agent_type is not None and resolved_agent_type != "generic":
            raise UserInputError(
                f"--command and --type are mutually exclusive. "
                f"Use --command to run a literal command (implicitly uses 'generic' agent type), "
                f"or use --type to specify an agent type like '{resolved_agent_type}'."
            )
        # Automatically use the "generic" agent type when --command is provided
        resolved_agent_type = "generic"

    is_clone = opts.source_agent is not None

    # Parse worktree base folder
    parsed_worktree_base_folder = Path(opts.worktree_base_folder).expanduser() if opts.worktree_base_folder else None

    agent_opts = CreateAgentOptions(
        agent_id=AgentId(opts.id) if opts.id else None,
        agent_type=AgentTypeName(resolved_agent_type) if resolved_agent_type else None,
        name=parsed_agent_name,
        command=CommandString(opts.command) if opts.command else None,
        additional_commands=tuple(NamedCommand.from_string(c) for c in opts.extra_window),
        agent_args=resolved_agent_args,
        target_path=parsed_target_path,
        worktree_base_folder=parsed_worktree_base_folder,
        transfer_mode=transfer_mode,
        initial_message=initial_message,
        data_options=data_options,
        git=git,
        environment=environment,
        lifecycle=lifecycle,
        permissions=permissions,
        label_options=label_options,
        provisioning=provisioning,
        source_agent_state_dir=source_agent_state_dir if is_clone else None,
    )
    return agent_opts, has_explicit_base


def _parse_host_lifecycle_options(opts: CreateCliOptions) -> HostLifecycleOptions:
    """Parse host lifecycle options from CLI args.

    These options control when a host is considered idle and should be shut down.
    They are separate from agent lifecycle options (like is_start_on_boot).
    """
    parsed_idle_mode = IdleMode(opts.idle_mode.upper()) if opts.idle_mode else None
    parsed_activity_sources = (
        tuple(ActivitySource(s.strip().upper()) for s in opts.activity_sources.split(","))
        if opts.activity_sources
        else None
    )
    parsed_idle_timeout = int(parse_duration_to_seconds(opts.idle_timeout)) if opts.idle_timeout is not None else None
    return HostLifecycleOptions(
        idle_timeout_seconds=parsed_idle_timeout,
        idle_mode=parsed_idle_mode,
        activity_sources=parsed_activity_sources,
    )


def _parse_target_host(
    opts: CreateCliOptions,
    address: AgentAddress,
    agent_and_host_loader: Callable[[], dict[DiscoveredHost, list[DiscoveredAgent]]],
    lifecycle: HostLifecycleOptions,
) -> DiscoveredHost | NewHostOptions | None:
    if not address.has_host_component:
        # No host specified in address, use local host
        return None

    is_new_host = _is_creating_new_host(address, opts.new_host)

    if is_new_host:
        # Creating a new host - provider is required
        if address.provider_name is None:
            raise UserInputError(
                "--new-host requires a provider in the agent address. "
                "Use NAME@HOST.PROVIDER --new-host or NAME@.PROVIDER."
            )

        # Parse host-level labels
        host_labels_dict: dict[str, str] = {}
        for label_string in opts.host_label:
            if "=" not in label_string:
                raise UserInputError(f"Host label must be in KEY=VALUE format, got: {label_string}")
            key, value = label_string.split("=", 1)
            host_labels_dict[key.strip()] = value.strip()

        # Parse host environment
        host_env_vars = resolve_env_vars(opts.pass_host_env, opts.host_env)
        host_env_files = tuple(Path(f) for f in opts.host_env_file)

        combined_build_args = _split_cli_args(opts.build_arg)
        combined_start_args = _split_cli_args(opts.start_arg)

        # Parse build options
        build_options = NewHostBuildOptions(
            snapshot=SnapshotName(opts.snapshot) if opts.snapshot else None,
            build_args=tuple(combined_build_args),
            start_args=tuple(combined_start_args),
        )

        parsed_host_name_style = HostNameStyle(opts.host_name_style.upper())
        return NewHostOptions(
            provider=address.provider_name,
            name=address.host_name,
            name_style=parsed_host_name_style,
            tags=host_labels_dict,
            build=build_options,
            environment=HostEnvironmentOptions(
                env_vars=host_env_vars,
                env_files=host_env_files,
            ),
            lifecycle=lifecycle,
        )

    # Targeting an existing host
    if address.host_name is None:
        # This shouldn't happen: has_host_component is True but host_name is None
        # means only provider_name is set, which _is_new_host_implied catches above
        raise UserInputError("Cannot target an existing host without a host name.")

    agents_by_host = agent_and_host_loader()
    all_hosts = list(agents_by_host.keys())

    host_ref = _find_existing_host(address.host_name, address.provider_name, all_hosts)
    if host_ref is None:
        raise UserInputError(f"Could not find host: {address.host_name}")

    return host_ref


def _find_existing_host(
    host_name: HostName,
    provider_name: ProviderInstanceName | None,
    all_hosts: list[DiscoveredHost],
) -> DiscoveredHost | None:
    """Look up an existing host by name or ID, using provider for disambiguation."""
    try:
        host_id = HostId(str(host_name))
        return get_host_from_list_by_id(host_id, all_hosts)
    except ValueError:
        pass

    matching = [h for h in all_hosts if h.host_name == host_name]

    # Use provider for disambiguation when there are multiple matches
    if len(matching) > 1 and provider_name is not None:
        filtered = [h for h in matching if h.provider_name == provider_name]
        if filtered:
            matching = filtered

    match len(matching):
        case 0:
            return None
        case 1:
            return matching[0]
        case _:
            host_list = ", ".join(f"{h.host_name} ({h.provider_name})" for h in matching)
            raise UserInputError(
                f"Multiple hosts found with name '{host_name}': {host_list}. "
                "Add .PROVIDER to the address for disambiguation (e.g., NAME@HOST.PROVIDER)."
            )


# === Parsing Functions ===


@pure
def _parse_branch_flag(branch: str, agent_name: AgentName) -> tuple[str | None, str | None, bool]:
    """Parse a --branch flag value in [BASE_BRANCH][:NEW_BRANCH] format.

    Returns (base_branch, new_branch_name, has_explicit_base) where:
    - base_branch is None if not specified (meaning "current branch")
    - new_branch_name is None if no colon is present (meaning "no new branch")
    - new_branch_name has any * replaced with the agent name
    - has_explicit_base is True if a non-empty base branch was specified
    """
    if ":" not in branch:
        # No colon: just a base branch, no new branch
        return (branch or None, None, bool(branch))

    base, new = branch.split(":", 1)
    if not new:
        new = _DEFAULT_NEW_BRANCH_PATTERN
    if new.count("*") > 1:
        raise UserInputError("--branch: at most one '*' is allowed in the new branch name")

    resolved_new = new.replace("*", str(agent_name))
    return (base or None, resolved_new, bool(base))


class ParsedSourceString(FrozenModel):
    """Result of parsing a source string in [AGENT[@HOST[.PROVIDER]]]:PATH format."""

    path: Path | None = Field(description="Path component")
    agent_name: str | None = Field(description="Agent name component")
    host_name: str | None = Field(description="Host name component (may include .PROVIDER suffix)")


@pure
def _parse_source_string(source_str: str) -> ParsedSourceString:
    """Parse [AGENT[@[HOST][.PROVIDER]]][:PATH] format into components.

    Without a colon and without '@', the string is treated as a plain path
    (the common case for --source ./some/dir). When '@' is present, the string
    is parsed as an agent address regardless of whether a colon follows. With a
    colon, the part before the colon is the address and the part after is the path.

    The host_name field may include a .PROVIDER suffix (e.g. "myhost.modal").
    """
    if ":" not in source_str:
        if "@" in source_str:
            # Agent address without a path (e.g. "my-agent@my-host")
            address = parse_agent_address(source_str)
            host_str = _host_str_from_address_components(address.host_name, address.provider_name)
            return ParsedSourceString(
                path=None,
                agent_name=str(address.agent_name) if address.agent_name else None,
                host_name=host_str,
            )
        # No colon or @ -- treat as a plain path (most common case: --source ./dir)
        return ParsedSourceString(path=Path(source_str), agent_name=None, host_name=None)

    prefix, path_str = source_str.split(":", 1)
    path = Path(path_str) if path_str else None

    if not prefix:
        return ParsedSourceString(path=path, agent_name=None, host_name=None)

    address = parse_agent_address(prefix)
    host_str = _host_str_from_address_components(address.host_name, address.provider_name)
    return ParsedSourceString(
        path=path,
        agent_name=str(address.agent_name) if address.agent_name else None,
        host_name=host_str,
    )


@pure
def _host_str_from_address_components(
    host_name: HostName | None, provider_name: ProviderInstanceName | None
) -> str | None:
    """Combine host and provider name components into a single host string."""
    if host_name is not None and provider_name is not None:
        return f"{host_name}.{provider_name}"
    if host_name is not None:
        return str(host_name)
    if provider_name is not None:
        return f".{provider_name}"
    return None


# === Helper Functions (stubs) ===


def _apply_host_labels(host: OnlineHostInterface, label_strings: tuple[str, ...]) -> None:
    """Parse KEY=VALUE host label strings and apply them to an existing host."""
    labels_to_add: dict[str, str] = {}
    for label_string in label_strings:
        if "=" in label_string:
            key, value = label_string.split("=", 1)
            labels_to_add[key.strip()] = value.strip()
    if labels_to_add:
        host.add_tags(labels_to_add)


def _ensure_clean_work_dir(location: HostLocation) -> None:
    """Verify the source work_dir has no uncommitted changes."""
    result = location.host.execute_idempotent_command("git status --porcelain", cwd=location.path)
    if not result.success:
        # Not a git repo or git command failed, skip the check
        logger.debug("Failed to check git status: {}", result.stderr)
        return

    if result.stdout.strip():
        raise UserInputError(
            f"Working tree at {location.path} has uncommitted changes. "
            "Use --no-ensure-clean to proceed anyway, or commit/stash your changes first."
        )


def _assemble_result(
    agent_id: AgentId,
    host_id: HostId,
) -> tuple[AgentId, HostId]:
    """Assemble the result for output."""
    return (agent_id, host_id)


def _find_agent_in_host(host: OnlineHostInterface, agent_id: AgentId) -> AgentInterface:
    """Find an agent by ID in a host."""
    for agent in host.get_agents():
        if agent.id == agent_id:
            return agent

    raise AgentNotFoundError(str(agent_id))


def _output_result(result: CreateAgentResult, opts: OutputOptions) -> None:
    """Output the create result according to output options."""
    if opts.is_quiet:
        return

    result_data = {"agent_id": str(result.agent.id), "host_id": str(result.host.id)}
    match opts.output_format:
        case OutputFormat.JSON:
            emit_final_json(result_data)
        case OutputFormat.JSONL:
            emit_event("created", result_data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            write_human_line("Done.")
        case _ as unreachable:
            assert_never(unreachable)


# Register help metadata for git-style help formatting
_CREATE_HELP_METADATA = CommandHelpMetadata(
    key="create",
    one_line_description="Create and run an agent",
    synopsis="""mngr [create|c] [<ADDRESS>] [<AGENT_TYPE>] [-t <TEMPLATE>] [--new-host] [-w WINDOW_NAME=COMMAND]
    [--label KEY=VALUE] [--host-label KEY=VALUE] [--project <PROJECT>] [--from <SOURCE>] [--transfer <MODE>]
    [--[no-]rsync] [--rsync-args <ARGS>] [--branch [BASE][:NEW]] [--[no-]ensure-clean]
    [--snapshot <ID>] [-b <BUILD_ARG>] [-s <START_ARG>]
    [--env <KEY=VALUE>] [--env-file <FILE>] [--grant <PERMISSION>] [--extra-provision-command <COMMAND>] [--upload-file <LOCAL:REMOTE>]
    [--idle-timeout <SECONDS>] [--idle-mode <MODE>] [--start-on-boot|--no-start-on-boot] [--reuse|--no-reuse]
    [--[no-]connect] [--[no-]auto-start] [--] [<AGENT_ARGS>...]""",
    aliases=("c",),
    arguments_description="""- `ADDRESS`: Agent address in `[NAME][@[HOST][.PROVIDER]]` format (all parts optional):
  - `NAME` -- agent name only, creates on local host (default)
  - `NAME@HOST` -- agent on existing host
  - `NAME@HOST.PROVIDER` -- agent on existing host (with provider for disambiguation)
  - `NAME@.PROVIDER` -- agent on a new host (auto-generated host name); implies `--new-host`
  - `NAME@HOST.PROVIDER --new-host` -- agent on a new host with the given name
- `AGENT_TYPE`: Which type of agent to run (default: `claude`). Can also be specified via `--type`
- `AGENT_ARGS`: Additional arguments passed to the agent""",
    description="""This command sets up an agent's working directory, optionally provisions a
new host (or uses an existing one), runs the specified agent process, and
connects to it by default.

By default, agents run locally in a new git worktree (for git repositories)
or an rsync copy (for non-git projects). Specify a host in the agent address
(e.g. NAME@HOST.PROVIDER) to target a remote host, or use NAME@.PROVIDER
to create a new one.

The agent type defaults to 'claude' if not specified. Any command in your
PATH can also be used as an agent type. Arguments after -- are passed
directly to the agent command.

For local agents in git repos, mngr creates a git worktree that shares objects
with your original repository. For remote agents, the repo is transferred
via git push --mirror. Use --transfer to override the default.""",
    examples=(
        ("Create an agent locally in a new git worktree (default)", "mngr create my-agent"),
        ("Create an agent in a new Docker container", "mngr create my-agent@.docker"),
        ("Create an agent in a new Modal sandbox", "mngr create my-agent@.modal"),
        ("Create using a named template", "mngr create my-agent --template modal"),
        ("Stack multiple templates", "mngr create my-agent -t modal -t codex"),
        ("Create a codex agent instead of claude", "mngr create my-agent codex"),
        ("Pass arguments to the agent", "mngr create my-agent -- --model opus"),
        ("Create on an existing host", "mngr create my-agent@my-dev-box"),
        ("Create on existing host with provider", "mngr create my-agent@my-dev-box.modal"),
        ("Create a new named host", "mngr create my-agent@my-host.modal --new-host"),
        ("Clone from an existing agent", "mngr create new-agent --source other-agent"),
        ("Run directly in-place (no transfer)", "mngr create my-agent --transfer=none"),
        ("Create without connecting", "mngr create my-agent --no-connect"),
        ("Add extra tmux windows", 'mngr create my-agent -w server="npm run dev"'),
        ("Reuse existing agent or create if not found", "mngr create my-agent --reuse"),
    ),
    see_also=(
        ("connect", "Connect to an existing agent"),
        ("list", "List existing agents"),
        ("destroy", "Destroy agents"),
    ),
    group_intros=(
        (
            "Connection Options",
            "See [connect options](./connect.md) for full details (only applies if `--connect` is specified).",
        ),
        (
            "Agent Provisioning",
            "See [Provision Options](../secondary/provision.md) for full details.",
        ),
        (
            "Host Options",
            "By default, `mngr create` uses the local host. Use the agent address to specify a different host.",
        ),
    ),
    additional_sections=(
        (
            "Agent Limits",
            "See [Limit Options](../secondary/limit.md)",
        ),
    ),
)

_CREATE_HELP_METADATA.register()

# Add pager-enabled help option to the create command
add_pager_help_option(create)
