import os
import shlex
import sys
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
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
from imbue.mng.api.connect import connect_to_agent
from imbue.mng.api.connect import resolve_connect_command
from imbue.mng.api.connect import run_connect_command
from imbue.mng.api.create import create as api_create
from imbue.mng.api.data_types import ConnectionOptions
from imbue.mng.api.data_types import CreateAgentResult
from imbue.mng.api.data_types import SourceLocation
from imbue.mng.api.discover import discover_all_hosts_and_agents
from imbue.mng.api.find import ensure_agent_started
from imbue.mng.api.find import ensure_host_started
from imbue.mng.api.find import get_host_from_list_by_id
from imbue.mng.api.find import resolve_source_location
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.env_utils import resolve_env_vars
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.output_helpers import emit_event
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.config.data_types import CreateCliOptions
from imbue.mng.config.data_types import MngContext
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.errors import AgentNotFoundError
from imbue.mng.errors import UserInputError
from imbue.mng.hosts.host import HostLocation
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.data_types import HostLifecycleOptions
from imbue.mng.interfaces.host import AgentDataOptions
from imbue.mng.interfaces.host import AgentEnvironmentOptions
from imbue.mng.interfaces.host import AgentGitOptions
from imbue.mng.interfaces.host import AgentLabelOptions
from imbue.mng.interfaces.host import AgentLifecycleOptions
from imbue.mng.interfaces.host import AgentPermissionsOptions
from imbue.mng.interfaces.host import AgentProvisioningOptions
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import FileModificationSpec
from imbue.mng.interfaces.host import HostEnvironmentOptions
from imbue.mng.interfaces.host import NamedCommand
from imbue.mng.interfaces.host import NewHostBuildOptions
from imbue.mng.interfaces.host import NewHostOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.interfaces.host import UploadFileSpec
from imbue.mng.primitives import ActivitySource
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentNameStyle
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import DiscoveredAgent
from imbue.mng.primitives import DiscoveredHost
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostNameStyle
from imbue.mng.primitives import IdleMode
from imbue.mng.primitives import LOCAL_PROVIDER_NAME
from imbue.mng.primitives import LogLevel
from imbue.mng.primitives import OutputFormat
from imbue.mng.primitives import Permission
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import SnapshotName
from imbue.mng.primitives import WorkDirCopyMode
from imbue.mng.utils.duration import parse_duration_to_seconds
from imbue.mng.utils.editor import EditorSession
from imbue.mng.utils.git_utils import derive_project_name_from_path
from imbue.mng.utils.git_utils import find_git_worktree_root
from imbue.mng.utils.git_utils import get_current_git_branch
from imbue.mng.utils.logging import LoggingConfig
from imbue.mng.utils.logging import LoggingSuppressor
from imbue.mng.utils.name_generator import generate_agent_name

_DEFAULT_NEW_BRANCH_PATTERN: Final[str] = "mng/*"


class _CachedAgentHostLoader(MutableModel):
    """Lazy loader that caches agents grouped by host on first access."""

    mng_ctx: MngContext = Field(frozen=True, description="Manager context for loading agents")
    cached_result: dict[DiscoveredHost, list[DiscoveredAgent]] | None = Field(
        default=None, description="Cached loading result"
    )

    def __call__(self) -> dict[DiscoveredHost, list[DiscoveredAgent]]:
        if self.cached_result is None:
            self.cached_result = discover_all_hosts_and_agents(self.mng_ctx)[0]
        return self.cached_result


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


class AgentAddress(FrozenModel):
    """Parsed agent address from [NAME][@[HOST][.PROVIDER]] format.

    Used to specify an agent and optionally its target host and provider in a single
    positional argument. Examples:
      - "foo" -> agent named "foo", local host
      - "foo@myhost" -> agent "foo" on existing host "myhost"
      - "foo@myhost.modal" -> agent "foo" on existing host "myhost" (on modal provider)
      - "foo@.modal" -> agent "foo" on a new host with auto-generated name on modal
      - "@myhost.modal" -> auto-named agent on existing host "myhost" (on modal provider)
    """

    agent_name: AgentName | None = None
    host_name: HostName | None = None
    provider_name: ProviderInstanceName | None = None

    @property
    def is_new_host_implied(self) -> bool:
        """True when the address implies creating a new host (NAME@.PROVIDER form)."""
        return self.provider_name is not None and self.host_name is None

    @property
    def has_host_component(self) -> bool:
        """True when any host or provider info was specified in the address."""
        return self.host_name is not None or self.provider_name is not None

    def is_creating_new_host(self, new_host_flag: bool) -> bool:
        """Whether this address combined with the --new-host flag means creating a new host."""
        return new_host_flag or self.is_new_host_implied


@click.command()
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
    default="english",
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
    default="astronomy",
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
    help="Directory to use as work_dir root [AGENT | AGENT.HOST | AGENT.HOST:PATH | HOST:PATH]. Defaults to current dir if no other source args are given",
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
@optgroup.option("--target-path", help="Directory to mount source inside agent host. Incompatible with --in-place")
@optgroup.option(
    "--in-place", "in_place", is_flag=True, help="Run directly in source directory. Incompatible with --target-path"
)
@optgroup.option(
    "--copy",
    "copy_source",
    is_flag=True,
    help="Copy source to isolated directory before running [default for remote agents, and for local agents if not in a git repo]",
)
@optgroup.option(
    "--clone",
    is_flag=True,
    help="Create a git clone that shares objects with original repo (only works for local agents)",
)
@optgroup.option(
    "--worktree",
    is_flag=True,
    help="Create a git worktree that shares objects and index with original repo [default for local agents in a git repo]. Requires a new branch in --branch (which is the default)",
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
    "--user-command", "user_command", multiple=True, help="Run custom shell command during provisioning [repeatable]"
)
@optgroup.option(
    "--sudo-command",
    "sudo_command",
    multiple=True,
    help="Run custom shell command as root during provisioning [repeatable]",
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
    help="Command to run instead of the builtin connect. MNG_AGENT_NAME and MNG_SESSION_NAME env vars are set.",
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
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="create",
        command_class=CreateCliOptions,
    )
    logging_config: LoggingConfig = ctx.meta["logging_config"]

    # Parse agent address from the positional argument or --name flag.
    # Both accept agent addresses; they are equivalent but mutually exclusive.
    if opts.positional_name and opts.name:
        raise UserInputError("Cannot specify both a positional agent address and --name. Use one or the other.")
    address = _parse_agent_address(opts.positional_name or opts.name or "")

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
        mng_ctx = mng_ctx.model_copy_update(
            to_update(mng_ctx.field_ref().is_auto_approve, True),
        )

    # Setup (validation, editor session, source resolution, etc.)
    setup = _setup_create(mng_ctx, output_opts, opts, logging_config, address)

    # Create agent
    create_result, connection_opts = _create_agent(mng_ctx, output_opts, opts, setup)
    _post_create(create_result, connection_opts, opts, mng_ctx)
    _finish_create(create_result, setup, output_opts)


class _CreateSetup(FrozenModel):
    """Per-invocation state shared between _setup_create and _create_agent."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    address: AgentAddress = Field(description="Parsed agent address from the positional argument")
    initial_message: str | None = Field(
        description="Resolved initial message content (from --message or --message-file)"
    )
    editor_session: EditorSession | None = Field(default=None, description="Editor session for --edit-message")
    agent_and_host_loader: _CachedAgentHostLoader = Field(description="Lazy loader for agents grouped by host")
    source_location: HostLocation = Field(description="Resolved source location")
    project_name: str = Field(description="Project name for agent labels")
    host_lifecycle: HostLifecycleOptions = Field(description="Host lifecycle options")


def _setup_create(
    mng_ctx: MngContext,
    output_opts: OutputOptions,
    opts: CreateCliOptions,
    logging_config: LoggingConfig,
    address: AgentAddress,
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
    agent_and_host_loader = _CachedAgentHostLoader(mng_ctx=mng_ctx)

    # figure out where the source data is coming from
    source_location = _resolve_source_location(opts, agent_and_host_loader, mng_ctx, is_start_desired=opts.start_host)

    # figure out the project label, in case we need that
    project_name = _parse_project_name(source_location, opts, address, mng_ctx)

    # Parse host lifecycle options (these go on the host, not the agent)
    host_lifecycle = _parse_host_lifecycle_options(opts)

    return _CreateSetup(
        address=address,
        initial_message=initial_message,
        editor_session=editor_session,
        agent_and_host_loader=agent_and_host_loader,
        source_location=source_location,
        project_name=project_name,
        host_lifecycle=host_lifecycle,
    )


def _create_agent(
    mng_ctx: MngContext,
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
        project_name=setup.project_name,
        agent_and_host_loader=setup.agent_and_host_loader,
        lifecycle=setup.host_lifecycle,
    )

    # Parse agent options
    agent_opts, has_explicit_base = _parse_agent_opts(
        opts=opts,
        address=address,
        initial_message=setup.initial_message,
        source_location=setup.source_location,
        mng_ctx=mng_ctx,
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
            mng_ctx=mng_ctx,
            agent_and_host_loader=setup.agent_and_host_loader,
        )
        if reuse_result is not None:
            agent, host = reuse_result
            logger.info("Reusing existing agent: {}", agent.name)

            # Handle --edit-message if editor session was started,
            # or send initial message directly if --message/--message-file was provided
            with _editor_cleanup_scope(setup.editor_session):
                if setup.editor_session is not None:
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
    # Skip the check when using worktree mode with an explicit base branch, since the
    # agent will be created from that branch and uncommitted changes in the current
    # working tree are irrelevant.
    is_worktree_from_other_branch = (
        agent_opts.git is not None and agent_opts.git.copy_mode == WorkDirCopyMode.WORKTREE and has_explicit_base
    )
    if opts.ensure_clean and not is_worktree_from_other_branch:
        _ensure_clean_work_dir(setup.source_location)

    # figure out the target host (if we just have a reference)
    resolved_target_host = _resolve_target_host(target_host, mng_ctx, is_start_desired=opts.start_host)

    # Set host labels on existing hosts (for new hosts, labels are passed via NewHostOptions).
    # This ensures local hosts get any --host-label values.
    if isinstance(resolved_target_host, OnlineHostInterface):
        _apply_host_labels(resolved_target_host, opts.host_label)

    # Set the project as a label on the agent (labels are agent-level, not host-level)
    if setup.project_name:
        agent_opts = agent_opts.model_copy_update(
            to_update(
                agent_opts.field_ref().label_options,
                AgentLabelOptions(labels={**agent_opts.label_options.labels, "project": setup.project_name}),
            ),
        )

    # Call the API create function
    with _editor_cleanup_scope(setup.editor_session):
        create_result = api_create(
            source_location=setup.source_location,
            target_host=resolved_target_host,
            agent_options=agent_opts,
            mng_ctx=mng_ctx,
        )

        # If --edit-message was used, wait for editor and send the message
        if setup.editor_session is not None:
            _handle_editor_message(
                editor_session=setup.editor_session,
                agent=create_result.agent,
            )

    return create_result, connection_opts


def _post_create(
    create_result: CreateAgentResult,
    connection_opts: ConnectionOptions,
    opts: CreateCliOptions,
    mng_ctx: MngContext,
) -> None:
    """Post-creation: connect."""
    if opts.connect:
        resolved_connect_command = resolve_connect_command(opts.connect_command, mng_ctx)
        if resolved_connect_command is not None:
            session_name = f"{mng_ctx.config.prefix}{create_result.agent.name}"
            run_connect_command(
                resolved_connect_command,
                str(create_result.agent.name),
                session_name,
                is_local=create_result.host.is_local,
            )
        else:
            connect_to_agent(create_result.agent, create_result.host, mng_ctx, connection_opts)


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
def _editor_cleanup_scope(editor_session: EditorSession | None) -> Iterator[None]:
    """Ensure editor session cleanup and logging suppressor restoration on exit.

    Safe to nest: EditorSession.cleanup() is idempotent, and
    LoggingSuppressor.disable_and_replay() is a no-op when not suppressed.
    """
    try:
        yield
    finally:
        if editor_session is not None:
            editor_session.cleanup()
        if LoggingSuppressor.is_suppressed():
            LoggingSuppressor.disable_and_replay(clear_screen=True)


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


def _parse_project_name(
    source_location: HostLocation, opts: CreateCliOptions, address: AgentAddress, mng_ctx: MngContext
) -> str:
    if opts.project:
        return opts.project

    if not source_location.host.is_local:
        raise NotImplementedError(
            "Have to re-implement the below function so that it works via HostInterface calls instead!"
        )

    source_project = derive_project_name_from_path(source_location.path, mng_ctx.concurrency_group)

    # When creating a new host from an external source (--source-agent or --source-host),
    # validate that the project inferred from the source matches the project inferred from
    # the local working directory. If they differ, the user must specify --project explicitly
    # to avoid silently tagging the agent with the wrong project.
    is_external_source = opts.source_agent is not None or opts.source_host is not None
    is_creating_new_host = address.is_creating_new_host(opts.new_host)
    if is_external_source and is_creating_new_host:
        local_git_root = find_git_worktree_root(None, mng_ctx.concurrency_group)
        local_path = local_git_root if local_git_root is not None else Path(os.getcwd())
        local_project = derive_project_name_from_path(local_path, mng_ctx.concurrency_group)
        if source_project != local_project:
            raise UserInputError(
                f"Project mismatch: source infers project '{source_project}' but local directory infers "
                f"'{local_project}'. Use --project to specify which project name to use."
            )

    return source_project


def _try_reuse_existing_agent(
    agent_name: AgentName,
    provider_name: ProviderInstanceName | None,
    target_host_ref: DiscoveredHost | None,
    mng_ctx: MngContext,
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
            f"Multiple agents found with name '{agent_name}', using the first one. Specify --host to target a specific host."
        )

    host_ref, agent_ref = matching_agents[0]
    logger.debug("Found existing agent {} on host {}", agent_ref.agent_id, host_ref.host_name)

    # Get the provider and host
    provider = get_provider_instance(host_ref.provider_name, mng_ctx)
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
    mng_ctx: MngContext,
    *,
    is_start_desired: bool,
) -> HostLocation:
    # figure out the agent source data
    if opts.source is None and opts.source_agent is None and opts.source_host is None:
        # easy, source location is on current host
        source_path = opts.source_path
        if source_path is None:
            git_root = find_git_worktree_root(None, mng_ctx.concurrency_group)
            source_path = str(git_root) if git_root is not None else os.getcwd()
        provider = get_provider_instance(LOCAL_PROVIDER_NAME, mng_ctx)
        host = provider.get_host(HostName("localhost"))
        online_host, _ = ensure_host_started(host, is_start_desired=is_start_desired, provider=provider)
        source_location = HostLocation(
            host=online_host,
            path=Path(source_path),
        )
    else:
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
            provider = get_provider_instance(LOCAL_PROVIDER_NAME, mng_ctx)
            host = provider.get_host(HostName("localhost"))
            online_host, _ = ensure_host_started(host, is_start_desired=is_start_desired, provider=provider)
            source_location = HostLocation(host=online_host, path=Path(source_path))
        else:
            # Need full resolution across providers
            agents_by_host = agent_and_host_loader()
            source_location = resolve_source_location(
                opts.source,
                opts.source_agent,
                opts.source_host,
                opts.source_path,
                agents_by_host,
                mng_ctx,
                is_start_desired=is_start_desired,
            )
    return source_location


def _resolve_target_host(
    target_host: DiscoveredHost | NewHostOptions | None,
    mng_ctx: MngContext,
    *,
    is_start_desired: bool,
) -> OnlineHostInterface | NewHostOptions:
    resolved_target_host: OnlineHostInterface | NewHostOptions
    if target_host is None:
        # No host specified, use the local provider's default host
        provider = get_provider_instance(LOCAL_PROVIDER_NAME, mng_ctx)
        host = provider.get_host(HostName("localhost"))
        resolved_target_host, _ = ensure_host_started(host, is_start_desired=is_start_desired, provider=provider)
    elif isinstance(target_host, DiscoveredHost):
        provider = get_provider_instance(target_host.provider_name, mng_ctx)
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
        # Parse [AGENT[.HOST]]:PATH format
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


def _get_current_git_branch(source_location: HostLocation, mng_ctx: MngContext) -> str | None:
    if not source_location.host.is_local:
        raise NotImplementedError(
            "Have to re-implement this function so that it works via HostInterface calls instead!"
        )

    return get_current_git_branch(source_location.path, mng_ctx.concurrency_group)


def _is_git_repo(path: Path, cg: ConcurrencyGroup) -> bool:
    """Check if the given path is inside a git repository."""
    return find_git_worktree_root(path, cg) is not None


@pure
def _was_value_after_double_dash(value: str) -> bool:
    """Check if a value appears after -- in sys.argv.

    This helps detect when click incorrectly assigns a value that was meant
    to be part of agent_args (after --) to an optional positional argument.
    """
    if "--" not in sys.argv:
        return False
    dash_index = sys.argv.index("--")
    args_after_dash = sys.argv[dash_index + 1 :]
    return value in args_after_dash


@pure
def _split_cli_args(args: tuple[str, ...]) -> list[str]:
    """Shell-tokenize each CLI arg and flatten into a single list.

    Handles cases like -b "--cpu 16" where the shell passes "--cpu 16" as a
    single string that needs to be split into ["--cpu", "16"].
    """
    return [token for arg in args for token in shlex.split(arg)]


def _parse_agent_opts(
    opts: CreateCliOptions,
    address: AgentAddress,
    initial_message: str | None,
    source_location: HostLocation,
    mng_ctx: MngContext,
) -> tuple[CreateAgentOptions, bool]:
    # Get agent name from address (which incorporates both positional and --name),
    # otherwise auto-generate
    parsed_agent_name: AgentName
    if address.agent_name is not None:
        parsed_agent_name = address.agent_name
    else:
        parsed_name_style = AgentNameStyle(opts.name_style.upper())
        parsed_agent_name = generate_agent_name(parsed_name_style)

    # Determine copy_mode from CLI flags
    # Priority: explicit flags > default behavior
    # Default: worktree for local git repos, copy for non-git repos or remote hosts
    copy_mode: WorkDirCopyMode | None
    # None means "in-place" (no copy/clone/worktree)
    if opts.in_place:
        copy_mode = None
    elif opts.worktree:
        copy_mode = WorkDirCopyMode.WORKTREE
    elif opts.clone:
        copy_mode = WorkDirCopyMode.CLONE
    elif opts.copy_source:
        copy_mode = WorkDirCopyMode.COPY
    else:
        # No explicit flag, apply defaults based on context
        # When creating a new remote host, always use COPY
        # since WORKTREE only works when source and target are on the same host
        is_creating_new_host = address.is_creating_new_host(opts.new_host)
        is_creating_remote_host = (
            is_creating_new_host
            and address.provider_name is not None
            and address.provider_name.lower() != LOCAL_PROVIDER_NAME
        )
        if is_creating_remote_host:
            copy_mode = WorkDirCopyMode.COPY
        elif source_location.host.is_local:
            is_git_repo = _is_git_repo(source_location.path, mng_ctx.concurrency_group)
            if is_git_repo:
                copy_mode = WorkDirCopyMode.WORKTREE
            else:
                copy_mode = WorkDirCopyMode.COPY
        else:
            copy_mode = WorkDirCopyMode.COPY

    # Parse --branch flag: [BASE_BRANCH][:NEW_BRANCH]
    base_branch, new_branch_name, has_explicit_base = _parse_branch_flag(opts.branch, parsed_agent_name)

    # --worktree requires a new branch
    if copy_mode == WorkDirCopyMode.WORKTREE and new_branch_name is None:
        raise UserInputError("--worktree requires a new branch. Use --branch BASE:NEW instead of --branch BASE.")

    # if the user didn't specify whether to include unclean, then infer from ensure_clean
    if opts.include_unclean is None:
        is_include_unclean = False if opts.ensure_clean else True
    else:
        is_include_unclean = opts.include_unclean

    # Build git options (None if copy_mode is None, meaning --in-place)
    git: AgentGitOptions | None
    if copy_mode is None:
        git = None
    else:
        git = AgentGitOptions(
            copy_mode=copy_mode,
            base_branch=base_branch or _get_current_git_branch(source_location, mng_ctx),
            new_branch_name=new_branch_name,
            depth=opts.depth,
            shallow_since=opts.shallow_since,
            is_git_synced=opts.include_git,
            is_include_unclean=is_include_unclean,
            is_include_gitignored=opts.include_gitignored,
        )

    # parse source data options
    data_options = AgentDataOptions(
        is_rsync_enabled=bool(opts.rsync or opts.rsync_args or git is None),
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
    labels_dict: dict[str, str] = {}
    for label_string in opts.label:
        if "=" not in label_string:
            raise UserInputError(f"Label must be in KEY=VALUE format, got: {label_string}")
        key, value = label_string.split("=", 1)
        labels_dict[key.strip()] = value.strip()
    label_options = AgentLabelOptions(labels=labels_dict)

    # Parse provisioning options
    provisioning = AgentProvisioningOptions(
        user_commands=opts.user_command,
        sudo_commands=opts.sudo_command,
        upload_files=tuple(UploadFileSpec.from_string(f) for f in opts.upload_file),
        append_to_files=tuple(FileModificationSpec.from_string(f) for f in opts.append_to_file),
        prepend_to_files=tuple(FileModificationSpec.from_string(f) for f in opts.prepend_to_file),
    )

    # Parse target_path if provided
    parsed_target_path = Path(opts.target_path) if opts.target_path else None

    # Determine agent type: --type takes priority, then positional argument
    # However, click may incorrectly assign values after -- to positional_agent_type
    # instead of agent_args. We detect this by checking if the value appears after
    # -- in sys.argv and move it to agent_args if so.
    #
    # Special case: --command implies using the "generic" agent type, which simply
    # runs the provided command. If --type is also specified to something other
    # than "generic", that's an error (they are mutually exclusive).
    resolved_agent_type = opts.type
    resolved_agent_args = opts.agent_args

    if opts.positional_agent_type:
        # Check if -- was used and positional_agent_type came from after it
        was_after_separator = _was_value_after_double_dash(opts.positional_agent_type)
        if was_after_separator:
            # This was meant to be an agent arg, not an agent type
            resolved_agent_args = (opts.positional_agent_type,) + resolved_agent_args
        elif resolved_agent_type is None:
            # Use it as the agent type
            resolved_agent_type = opts.positional_agent_type
        else:
            # --type was already specified, ignore the positional (could warn here)
            pass

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

    agent_opts = CreateAgentOptions(
        agent_id=AgentId(opts.id) if opts.id else None,
        agent_type=AgentTypeName(resolved_agent_type) if resolved_agent_type else None,
        name=parsed_agent_name,
        command=CommandString(opts.command) if opts.command else None,
        additional_commands=tuple(NamedCommand.from_string(c) for c in opts.extra_window),
        agent_args=resolved_agent_args,
        target_path=parsed_target_path,
        initial_message=initial_message,
        data_options=data_options,
        git=git,
        environment=environment,
        lifecycle=lifecycle,
        permissions=permissions,
        label_options=label_options,
        provisioning=provisioning,
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
    project_name: str | None,
    agent_and_host_loader: Callable[[], dict[DiscoveredHost, list[DiscoveredAgent]]],
    lifecycle: HostLifecycleOptions,
) -> DiscoveredHost | NewHostOptions | None:
    if not address.has_host_component:
        # No host specified in address, use local host
        return None

    is_new_host = address.is_creating_new_host(opts.new_host)

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
        # means only provider_name is set, which is_new_host_implied catches above
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


@pure
def _parse_agent_address(address_str: str) -> AgentAddress:
    """Parse an agent address string into its components.

    Format: [AGENT_NAME][@[HOST_NAME][.PROVIDER_NAME]]

    The host part (after @) may contain at most one dot separating the host name
    from the provider name. Additional dots are not allowed.

    Examples:
      - "" -> everything None (auto-generate name, local host)
      - "foo" -> agent_name="foo"
      - "foo@myhost" -> agent_name="foo", host_name="myhost"
      - "foo@myhost.modal" -> agent_name="foo", host_name="myhost", provider_name="modal"
      - "foo@.modal" -> agent_name="foo", provider_name="modal" (implies new host)
      - "@myhost.modal" -> host_name="myhost", provider_name="modal" (auto-generate name)
    """
    if not address_str:
        return AgentAddress()

    if "@" not in address_str:
        # Simple agent name with no host component
        return AgentAddress(agent_name=AgentName(address_str))

    agent_part, host_part = address_str.split("@", 1)
    agent_name = AgentName(agent_part) if agent_part else None

    if not host_part:
        # "foo@" -> just agent name, no host component
        return AgentAddress(agent_name=agent_name)

    dot_count = host_part.count(".")
    if dot_count > 1:
        raise UserInputError(
            f"Invalid agent address: host part '{host_part}' contains more than one dot. "
            "Expected format: [NAME][@[HOST][.PROVIDER]]"
        )

    if dot_count == 1:
        host_str, provider_str = host_part.split(".", 1)
        host_name = HostName(host_str) if host_str else None
        provider_name = ProviderInstanceName(provider_str) if provider_str else None
    else:
        host_name = HostName(host_part)
        provider_name = None

    return AgentAddress(agent_name=agent_name, host_name=host_name, provider_name=provider_name)


class ParsedSourceString(FrozenModel):
    """Result of parsing a source string in [AGENT[.HOST]]:PATH format."""

    path: Path | None = Field(description="Path component")
    agent_name: str | None = Field(description="Agent name component")
    host_name: str | None = Field(description="Host name component")


@pure
def _parse_source_string(source_str: str) -> ParsedSourceString:
    """Parse [AGENT[.HOST]]:PATH format into components."""
    if ":" not in source_str:
        # Just a path
        return ParsedSourceString(path=Path(source_str), agent_name=None, host_name=None)

    prefix, path_str = source_str.rsplit(":", 1)
    path = Path(path_str) if path_str else None

    if "." in prefix:
        agent, host = prefix.split(".", 1)
        return ParsedSourceString(path=path, agent_name=agent or None, host_name=host or None)

    return ParsedSourceString(path=path, agent_name=prefix or None, host_name=None)


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
    result = location.host.execute_command("git status --porcelain", cwd=location.path)
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
    synopsis="""mng [create|c] [<ADDRESS>] [<AGENT_TYPE>] [-t <TEMPLATE>] [--new-host] [-w WINDOW_NAME=COMMAND]
    [--label KEY=VALUE] [--host-label KEY=VALUE] [--project <PROJECT>] [--from <SOURCE>] [--in-place|--copy|--clone|--worktree]
    [--[no-]rsync] [--rsync-args <ARGS>] [--branch [BASE][:NEW]] [--[no-]ensure-clean]
    [--snapshot <ID>] [-b <BUILD_ARG>] [-s <START_ARG>]
    [--env <KEY=VALUE>] [--env-file <FILE>] [--grant <PERMISSION>] [--user-command <COMMAND>] [--upload-file <LOCAL:REMOTE>]
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
or a copy of the current directory. Specify a host in the agent address
(e.g. NAME@HOST.PROVIDER) to target a remote host, or use NAME@.PROVIDER
to create a new one.

The agent type defaults to 'claude' if not specified. Any command in your
PATH can also be used as an agent type. Arguments after -- are passed
directly to the agent command.

For local agents, mng creates a git worktree that shares objects with your
original repository, allowing efficient branch management. For remote agents,
the working directory is copied to the remote host.""",
    examples=(
        ("Create an agent locally in a new git worktree (default)", "mng create my-agent"),
        ("Create an agent in a new Docker container", "mng create my-agent@.docker"),
        ("Create an agent in a new Modal sandbox", "mng create my-agent@.modal"),
        ("Create using a named template", "mng create my-agent --template modal"),
        ("Stack multiple templates", "mng create my-agent -t modal -t codex"),
        ("Create a codex agent instead of claude", "mng create my-agent codex"),
        ("Pass arguments to the agent", "mng create my-agent -- --model opus"),
        ("Create on an existing host", "mng create my-agent@my-dev-box"),
        ("Create on existing host with provider", "mng create my-agent@my-dev-box.modal"),
        ("Create a new named host", "mng create my-agent@my-host.modal --new-host"),
        ("Clone from an existing agent", "mng create new-agent --source other-agent"),
        ("Run directly in-place (no worktree)", "mng create my-agent --in-place"),
        ("Create without connecting", "mng create my-agent --no-connect"),
        ("Add extra tmux windows", 'mng create my-agent -w server="npm run dev"'),
        ("Reuse existing agent or create if not found", "mng create my-agent --reuse"),
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
            "By default, `mng create` uses the local host. Use the agent address to specify a different host.",
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
