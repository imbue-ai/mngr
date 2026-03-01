import os
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import assert_never
from typing import cast

import click
from click_option_group import optgroup
from loguru import logger
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
from imbue.mng.api.find import ensure_agent_started
from imbue.mng.api.find import ensure_host_started
from imbue.mng.api.find import get_host_from_list_by_id
from imbue.mng.api.find import get_unique_host_from_list_by_name
from imbue.mng.api.find import resolve_source_location
from imbue.mng.api.list import load_all_agents_grouped_by_host
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.cli.common_opts import CommonCliOptions
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.env_utils import resolve_env_vars
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.output_helpers import emit_event
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.config.data_types import MngContext
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.errors import AgentNotFoundError
from imbue.mng.errors import MngError
from imbue.mng.errors import UserInputError
from imbue.mng.hosts.host import Host
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
from imbue.mng.interfaces.host import DEFAULT_AGENT_READY_TIMEOUT_SECONDS
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
from imbue.mng.primitives import AgentReference
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostNameStyle
from imbue.mng.primitives import HostReference
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
from imbue.mng.utils.logging import remove_console_handlers
from imbue.mng.utils.name_generator import generate_agent_name
from imbue.mng.utils.polling import wait_for


class _CachedAgentHostLoader(MutableModel):
    """Lazy loader that caches agents grouped by host on first access."""

    mng_ctx: MngContext = Field(frozen=True, description="Manager context for loading agents")
    cached_result: dict[HostReference, list[AgentReference]] | None = Field(
        default=None, description="Cached loading result"
    )

    def __call__(self) -> dict[HostReference, list[AgentReference]]:
        if self.cached_result is None:
            self.cached_result = load_all_agents_grouped_by_host(self.mng_ctx)[0]
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


class CreateCliOptions(CommonCliOptions):
    """Options passed from the CLI to the create command.

    This captures all the click parameters so we can pass them as a single object
    to helper functions instead of passing dozens of individual parameters.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.

    Note that this class VERY INTENTIONALLY DOES NOT use Field() decorators with descriptions, defaults, etc.
    For that information, see the click.option() and click.argument() decorators on the create() function itself.
    """

    positional_name: str | None
    positional_agent_type: str | None
    agent_args: tuple[str, ...]
    template: tuple[str, ...]
    agent_type: str | None
    reuse: bool
    connect: bool
    connect_command: str | None
    await_ready: bool | None
    await_agent_stopped: bool | None
    copy_work_dir: bool | None
    ensure_clean: bool
    snapshot_source: bool | None
    name: str | None
    name_style: str
    agent_command: str | None
    add_command: tuple[str, ...]
    user: str | None
    source: str | None
    source_agent: str | None
    source_host: str | None
    source_path: str | None
    target: str | None
    target_path: str | None
    in_place: bool
    copy_source: bool
    clone: bool
    worktree: bool
    rsync: bool | None
    rsync_args: str | None
    include_git: bool
    include_unclean: bool | None
    include_gitignored: bool
    base_branch: str | None
    new_branch: str | None
    new_branch_prefix: str
    depth: int | None
    shallow_since: str | None
    agent_env: tuple[str, ...]
    agent_env_file: tuple[str, ...]
    pass_agent_env: tuple[str, ...]
    host: str | None
    new_host: str | None
    host_name: str | None
    host_name_style: str
    tag: tuple[str, ...]
    label: tuple[str, ...]
    project: str | None
    host_env: tuple[str, ...]
    host_env_file: tuple[str, ...]
    pass_host_env: tuple[str, ...]
    known_hosts: tuple[str, ...]
    authorized_keys: tuple[str, ...]
    snapshot: str | None
    build_arg: tuple[str, ...]
    build_args: str | None
    start_arg: tuple[str, ...]
    start_args: str | None
    reconnect: bool
    interactive: bool | None
    message: str | None
    message_file: str | None
    edit_message: bool
    resume_message: str | None
    resume_message_file: str | None
    retry: int
    retry_delay: str
    attach_command: str | None
    idle_timeout: str | None
    idle_mode: str | None
    activity_sources: str | None
    start_on_boot: bool | None
    start_host: bool
    grant: tuple[str, ...]
    user_command: tuple[str, ...]
    sudo_command: tuple[str, ...]
    upload_file: tuple[str, ...]
    append_to_file: tuple[str, ...]
    prepend_to_file: tuple[str, ...]
    create_directory: tuple[str, ...]
    ready_timeout: float
    yes: bool


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
@optgroup.option("-n", "--name", help="Agent name (alternative to positional argument) [default: auto-generated]")
@optgroup.option(
    "--name-style",
    type=click.Choice(_make_name_style_choices(), case_sensitive=False),
    default="english",
    show_default=True,
    help="Auto-generated name style",
)
@optgroup.option("--agent-type", help="Which type of agent to run [default: claude]")
@optgroup.option(
    "--agent-cmd",
    "--agent-command",
    "agent_command",
    help="Run a literal command using the generic agent type (mutually exclusive with --agent-type)",
)
# FOLLOWUP: hmm... I wonder if the name of this should be changed to something more like "window" to be more closely aligned with the tmux primitive it actually creates...
#  more generally, we probably need to do a pass at refining *all* of these option names...
@optgroup.option(
    "-c",
    "--add-cmd",
    "--add-command",
    "add_command",
    multiple=True,
    help='Run extra command in additional window. Use name="command" to set window name. Note: ALL_UPPERCASE names (e.g., FOO="bar") are treated as env var assignments, not window names',
)
@optgroup.option(
    "--user",
    help="Override which user to run the agent as [default: current user for local, provider-defined or root for remote]",
)
@optgroup.group("Host Options")
@optgroup.option("--in", "--new-host", "new_host", help="Create a new host using provider (docker, modal, ...)")
@optgroup.option("--host", "--target-host", help="Use an existing host (by name or ID) [default: local]")
@optgroup.option(
    "--project",
    help="Project name for the agent (sets the 'project' label) [default: derived from git remote origin or folder name]",
)
@optgroup.option("--label", multiple=True, help="Agent label KEY=VALUE [repeatable] [experimental]")
@optgroup.option("--tag", multiple=True, help="Host metadata tag KEY=VALUE [repeatable]")
@optgroup.option("--host-name", help="Name for the new host")
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
    "--await-ready/--no-await-ready",
    "await_ready",
    default=None,
    help="Wait until agent is ready before returning [default: no-await-ready if --no-connect]",
)
@optgroup.option(
    "--await-agent-stopped/--no-await-agent-stopped",
    "await_agent_stopped",
    default=None,
    help="Wait until agent has completely finished running before exiting. Useful for testing and scripting. First waits for agent to become ready, then waits for it to stop. [default: no-await-agent-stopped]",
)
@optgroup.option(
    "--ensure-clean/--no-ensure-clean", default=True, show_default=True, help="Abort if working tree is dirty"
)
@optgroup.option(
    "--snapshot-source/--no-snapshot-source",
    "snapshot_source",
    default=None,
    help="Snapshot source agent first [default: yes if --source-agent and not local]",
)
@optgroup.option(
    "--copy-work-dir/--no-copy-work-dir",
    "copy_work_dir",
    default=None,
    help="Copy source work_dir immediately. Useful when launching background agents so you can continue editing locally without changes being copied to the new agent [default: copy if --no-connect, no-copy if --connect]",
)
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
    help="Create a git worktree that shares objects and index with original repo [default for local agents in a git repo]. Requires --new-branch (which is the default)",
)
@optgroup.group("Agent Git Configuration")
@optgroup.option("--base-branch", help="The starting point for the agent [default: current branch]")
@optgroup.option(
    "--new-branch",
    "new_branch",
    is_flag=False,
    flag_value="",
    default="",
    help="Create a fresh branch (named TEXT if provided, otherwise auto-generated) [default: new branch]",
)
@optgroup.option(
    "--no-new-branch",
    "new_branch",
    flag_value=None,
    is_flag=True,
    help="Do not create a new branch; use the current branch directly. Incompatible with --worktree",
)
@optgroup.option(
    "--new-branch-prefix", default="mng/", show_default=True, help="Prefix for auto-generated branch names"
)
@optgroup.option("--depth", type=int, help="Shallow clone depth [default: full]")
@optgroup.option("--shallow-since", help="Shallow clone since date")
@optgroup.group("Agent Environment Variables")
@optgroup.option("--env", "--agent-env", "agent_env", multiple=True, help="Set environment variable KEY=VALUE")
@optgroup.option(
    "--env-file",
    "--agent-env-file",
    "agent_env_file",
    type=click.Path(exists=True),
    multiple=True,
    help="Load env",
)
@optgroup.option("--pass-env", "--pass-agent-env", "pass_agent_env", multiple=True, help="Forward variable from shell")
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
@optgroup.option(
    "--create-directory", "create_directory", multiple=True, help="Create directory on remote [repeatable]"
)
@optgroup.group("New Host Environment Variables")
@optgroup.option("--host-env", multiple=True, help="Set environment variable KEY=VALUE for host [repeatable]")
@optgroup.option(
    "--host-env-file", type=click.Path(exists=True), multiple=True, help="Load env file for host [repeatable]"
)
@optgroup.option("--pass-host-env", multiple=True, help="Forward variable from shell for host [repeatable]")
@optgroup.option(
    "--known-host",
    "known_hosts",
    multiple=True,
    help="SSH known_hosts entry to add to the host (for outbound SSH) [repeatable]",
)
@optgroup.option(
    "--authorized-key",
    "authorized_keys",
    multiple=True,
    help="SSH authorized_keys entry to add to the host (for inbound SSH) [repeatable]",
)
@optgroup.group("New Host Build")
@optgroup.option("--snapshot", help="Use existing snapshot instead of building")
@optgroup.option(
    "-b",
    "--build",
    "--build-arg",
    "build_arg",
    multiple=True,
    help="Build argument as key=value or --key=value (e.g., -b gpu=h100 -b cpu=2) [repeatable]",
)
@optgroup.option("--build-args", help="Space-separated build arguments (e.g., 'gpu=h100 cpu=2')")
@optgroup.option("-s", "--start", "--start-arg", "start_arg", multiple=True, help="Argument for start [repeatable]")
@optgroup.option("--start-args", help="Space-separated start arguments (alternative to -s)")
@optgroup.group("New Host Lifecycle")
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
@optgroup.option("--resume-message", help="Message to send when the agent is started (resumed) after being stopped")
@optgroup.option(
    "--resume-message-file", type=click.Path(exists=True), help="File containing resume message to send on start"
)
@optgroup.option(
    "--ready-timeout",
    type=float,
    default=DEFAULT_AGENT_READY_TIMEOUT_SECONDS,
    show_default=True,
    help="Timeout in seconds to wait for agent readiness before sending initial message",
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

    # Apply --yes flag to auto-approve prompts (e.g., skill installation)
    if opts.yes:
        mng_ctx = mng_ctx.model_copy_update(
            to_update(mng_ctx.field_ref().is_auto_approve, True),
        )

    result = _handle_create(mng_ctx, output_opts, opts, logging_config)
    if result is None:
        return
    create_result, connection_opts, output_opts, opts, mng_ctx = result
    _post_create(create_result, connection_opts, output_opts, opts, mng_ctx)


def _handle_create(
    mng_ctx: MngContext,
    output_opts: OutputOptions,
    opts: CreateCliOptions,
    logging_config: LoggingConfig,
) -> tuple[CreateAgentResult, ConnectionOptions, OutputOptions, CreateCliOptions, MngContext] | None:
    # Validate that both --message and --message-file are not provided
    if opts.message is not None and opts.message_file is not None:
        raise UserInputError("Cannot provide both --message and --message-file")

    # Validate that both --resume-message and --resume-message-file are not provided
    if opts.resume_message is not None and opts.resume_message_file is not None:
        raise UserInputError("Cannot provide both --resume-message and --resume-message-file")

    # validate that we're not waiting for the agent to stop and trying to connect:
    if opts.await_agent_stopped and opts.connect:
        raise UserInputError(
            "Cannot use --await-agent-stopped and --connect together. Pass --no-connect to just wait."
        )

    # Early validation: --edit-message cannot be used with background creation
    # Background creation happens when --no-connect and --no-await-ready (the default when --no-connect)
    # We check this BEFORE creating the editor session to avoid starting an editor subprocess
    # that would immediately need to be cleaned up (which causes race conditions and flaky tests)
    if opts.edit_message:
        # Compute should_await_ready the same way it's computed later
        early_should_await_ready = opts.await_ready
        if early_should_await_ready is None:
            if opts.await_agent_stopped:
                early_should_await_ready = True
            else:
                early_should_await_ready = opts.connect
        # Check if this would be background creation
        if not opts.connect and not early_should_await_ready:
            raise UserInputError(
                "--edit-message cannot be used with background creation (--no-connect --no-await-ready). "
                "Use --await-ready to wait for agent creation."
            )

    # Read message from file if --message-file is provided (used as initial content for editor if --edit-message)
    initial_message_content: str | None
    if opts.message_file is not None:
        message_file_path = Path(opts.message_file)
        initial_message_content = message_file_path.read_text()
    elif opts.message is not None:
        initial_message_content = opts.message
    else:
        initial_message_content = None

    # Read resume message from file if --resume-message-file is provided
    resume_message_content: str | None
    if opts.resume_message_file is not None:
        resume_message_file_path = Path(opts.resume_message_file)
        resume_message_content = resume_message_file_path.read_text()
    elif opts.resume_message is not None:
        resume_message_content = opts.resume_message
    else:
        resume_message_content = None

    # If --edit-message is set, start the editor immediately
    # The editor runs in parallel with agent creation
    # We suppress logging while the editor is open to avoid writing to the terminal
    editor_session: EditorSession | None = None
    if opts.edit_message:
        editor_session = EditorSession.create(initial_content=initial_message_content)
        # Enable logging suppression before starting the editor so that
        # log messages don't interfere with the editor's terminal output
        LoggingSuppressor.enable(logging_config.console_level, logging_config.log_level)
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
    project_name = _parse_project_name(source_location, opts, mng_ctx)

    # Parse host lifecycle options (these go on the host, not the agent)
    host_lifecycle = _parse_host_lifecycle_options(opts)

    # Parse target host (existing or new)
    target_host = _parse_target_host(
        opts=opts,
        project_name=project_name,
        agent_and_host_loader=agent_and_host_loader,
        lifecycle=host_lifecycle,
    )

    # Parse agent options
    agent_opts = _parse_agent_opts(
        opts=opts,
        initial_message=initial_message,
        resume_message=resume_message_content,
        source_location=source_location,
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

    # at this point, all options are parsed, and we can actually start the executing the command

    # If --reuse is set, try to find and reuse an existing agent with the same name
    if opts.reuse and agent_opts.name is not None:
        reuse_result = _try_reuse_existing_agent(
            agent_name=agent_opts.name,
            provider_name=ProviderInstanceName(opts.new_host) if opts.new_host else None,
            target_host_ref=target_host if isinstance(target_host, HostReference) else None,
            mng_ctx=mng_ctx,
            agent_and_host_loader=agent_and_host_loader,
        )
        if reuse_result is not None:
            agent, host = reuse_result
            logger.info("Reusing existing agent: {}", agent.name)

            # Handle --edit-message if editor session was started,
            # or send initial message directly if --message/--message-file was provided
            try:
                if editor_session is not None:
                    _handle_editor_message(
                        editor_session=editor_session,
                        agent=agent,
                    )
                elif initial_message is not None:
                    # Send initial message directly (from --message or --message-file)
                    logger.info("Sending message to agent")
                    agent.send_message(initial_message)
                else:
                    pass
            finally:
                # Clean up editor session on success or failure
                if editor_session is not None and not editor_session.is_finished():
                    editor_session.cleanup()
                # Ensure logging suppression is disabled on any exit path
                if LoggingSuppressor.is_suppressed():
                    LoggingSuppressor.disable_and_replay(clear_screen=True)

            create_result = CreateAgentResult(agent=agent, host=host)
            return create_result, connection_opts, output_opts, opts, mng_ctx

    # If ensure-clean is set, verify the source work_dir is clean.
    # Skip the check when an explicit --base-branch is provided, since the agent
    # will be created from that branch and uncommitted changes in the current
    # working tree are irrelevant (regardless of copy mode).
    is_from_explicit_base_branch = agent_opts.git is not None and opts.base_branch is not None
    if opts.ensure_clean and not is_from_explicit_base_branch:
        _ensure_clean_work_dir(source_location)

    # figure out the target host (if we just have a reference)
    resolved_target_host = _resolve_target_host(target_host, mng_ctx, is_start_desired=opts.start_host)

    # Set tags on existing hosts (for new hosts, tags are passed via NewHostOptions).
    # This ensures local hosts get any --tag values.
    if isinstance(resolved_target_host, OnlineHostInterface):
        tags_to_add: dict[str, str] = {}
        for tag_string in opts.tag:
            if "=" in tag_string:
                key, value = tag_string.split("=", 1)
                tags_to_add[key.strip()] = value.strip()
        if tags_to_add:
            resolved_target_host.add_tags(tags_to_add)

    # Set the project as a label on the agent (labels are agent-level, not host-level)
    if project_name:
        agent_opts = agent_opts.model_copy_update(
            to_update(
                agent_opts.field_ref().label_options,
                AgentLabelOptions(labels={**agent_opts.label_options.labels, "project": project_name}),
            ),
        )

    # figure out the source (this may snapshot the source agent if needed)
    snapshot = _snapshot_if_required(
        mng_ctx=mng_ctx,
        snapshot_source=opts.snapshot_source,
        source_location=source_location,
    )

    # create work_dir immediately (if necessary)
    # note that this only matters if we're NOT using a snapshot, otherwise it's already "copied"
    # and obviously only matters if we're not creating a new host
    is_work_dir_created: bool
    early_created_branch_name: str | None = None
    if snapshot is None and agent_opts.is_copy_immediate and isinstance(resolved_target_host, OnlineHostInterface):
        work_dir_result = resolved_target_host.create_agent_work_dir(
            source_location.host, source_location.path, agent_opts
        )
        # Record the actual work_dir path in agent_opts so the API uses it
        # (the path may have been auto-generated, e.g. for worktrees)
        agent_opts = agent_opts.model_copy_update(
            to_update(agent_opts.field_ref().target_path, work_dir_result.path),
        )
        early_created_branch_name = work_dir_result.created_branch_name
        is_work_dir_created = True
    else:
        if snapshot is None:
            is_work_dir_created = False
        else:
            is_work_dir_created = True

    # Determine whether to wait for agent to be ready
    # Default: --no-await-ready when --no-connect, --await-ready when --connect
    # Note: --await-agent-stopped implies --await-ready (we need the agent to be ready first)
    should_await_ready = opts.await_ready
    if should_await_ready is None:
        if opts.await_agent_stopped:
            should_await_ready = True
        else:
            should_await_ready = opts.connect

    # If --no-connect and --no-await-ready, run api_create in background
    # Note: --edit-message incompatibility is validated early (before editor creation) to avoid
    # starting an editor subprocess that would need to be cleaned up
    if not opts.connect and not should_await_ready:
        _create_agent_in_background(
            source_location,
            resolved_target_host,
            agent_opts,
            mng_ctx,
            is_work_dir_created,
            output_opts,
            created_branch_name=early_created_branch_name,
        )
        return

    # Call the API create function (synchronously)
    # Wrap in try/finally to ensure editor cleanup on failure
    try:
        create_result = api_create(
            source_location=source_location,
            target_host=resolved_target_host,
            agent_options=agent_opts,
            mng_ctx=mng_ctx,
            create_work_dir=not is_work_dir_created,
            created_branch_name=early_created_branch_name,
        )

        # If --edit-message was used, wait for editor and send the message
        if editor_session is not None:
            _handle_editor_message(
                editor_session=editor_session,
                agent=create_result.agent,
            )
    finally:
        # Clean up editor session on success or failure
        if editor_session is not None and not editor_session.is_finished():
            editor_session.cleanup()
        # Ensure logging suppression is disabled on any exit path
        if LoggingSuppressor.is_suppressed():
            LoggingSuppressor.disable_and_replay(clear_screen=True)

    return create_result, connection_opts, output_opts, opts, mng_ctx


def _post_create(
    create_result: CreateAgentResult,
    connection_opts: ConnectionOptions,
    output_opts: OutputOptions,
    opts: CreateCliOptions,
    mng_ctx: MngContext,
) -> None:
    # If --await-agent-stopped is set, wait for the agent to finish running
    if opts.await_agent_stopped:
        _await_agent_stopped(create_result.agent)

    # If --connect is set, connect to the agent (or run the custom connect command)
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

    # output the result
    _output_result(create_result, output_opts)


def _on_editor_exit() -> None:
    """Callback invoked when the editor process exits.

    Restores logging by disabling suppression and replaying buffered messages.
    This is called from a background thread as soon as the editor exits.
    """
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
    try:
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

    finally:
        editor_session.cleanup()
        # Make sure suppression is disabled even if an exception occurred
        # (e.g., if the callback wasn't called for some reason)
        if LoggingSuppressor.is_suppressed():
            LoggingSuppressor.disable_and_replay(clear_screen=True)


def _create_agent_in_background(
    source_location: HostLocation,
    target_host: OnlineHostInterface | NewHostOptions,
    agent_options: CreateAgentOptions,
    mng_ctx: MngContext,
    is_work_dir_created: bool,
    output_opts: OutputOptions,
    created_branch_name: str | None = None,
) -> None:
    """Create an agent in a background process that continues after parent exits.

    This function forks the current process. The parent exits immediately while
    the child process continues to run api_create() in the background.
    """
    pid = os.fork()

    if pid > 0:
        # Parent process: output message and exit immediately
        logger.info("Agent creation started in background (PID: {})", pid)
        logger.info("Agent name: {}", agent_options.name)
        return

    # Child process: detach from parent and continue
    try:
        # Create a new session to detach from parent's terminal
        os.setsid()

        # Remove console handlers from loguru to prevent "I/O operation on closed file"
        # errors when the parent's terminal closes. File logging continues to work.
        remove_console_handlers()

        # Call the API create function
        create_result = api_create(
            source_location=source_location,
            target_host=target_host,
            agent_options=agent_options,
            mng_ctx=mng_ctx,
            create_work_dir=not is_work_dir_created,
            created_branch_name=created_branch_name,
        )

        # Output result
        _output_result(create_result, output_opts)

        # Exit the child process
        os._exit(0)
    except MngError as e:
        # Log the error and exit with non-zero status
        logger.error("Failed to create agent in background: {}", e)
        os._exit(1)


def _parse_project_name(source_location: HostLocation, opts: CreateCliOptions, mng_ctx: MngContext) -> str:
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
    is_creating_new_host = opts.new_host is not None
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
    target_host_ref: HostReference | None,
    mng_ctx: MngContext,
    agent_and_host_loader: Callable[[], dict[HostReference, list[AgentReference]]],
) -> tuple[AgentInterface, OnlineHostInterface] | None:
    """Try to find and start an existing agent with the given name.

    Searches for an agent matching the name, scoped by provider and host if specified.
    If found, ensures the agent is started and returns it along with its host.
    If not found, returns None so the caller can proceed with creating a new agent.
    """
    agents_by_host = agent_and_host_loader()

    matching_agents: list[tuple[HostReference, AgentReference]] = []

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
    agent_and_host_loader: Callable[[], dict[HostReference, list[AgentReference]]],
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
    target_host: HostReference | NewHostOptions | None,
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
    elif isinstance(target_host, HostReference):
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


def _snapshot_if_required(
    mng_ctx: MngContext,
    snapshot_source: bool | None,
    source_location: HostLocation,
) -> str | None:
    # Determine if we need to snapshot the source agent
    snapshot_name: str | None = None
    should_snapshot = snapshot_source
    # Default to snapshotting if:
    # 1. source is a remote agent
    # 2. whose provider can snapshot
    # 3. and the user didn't explicitly disable
    is_remote_agent = not source_location.host.is_local
    # Cast to Host to access provider_instance (implementation detail)
    host = cast(Host, source_location.host)
    is_provider_able_to_snapshot = host.provider_instance.supports_snapshots
    is_snapshot_behavior_specified_by_user = should_snapshot is not None
    if is_remote_agent and is_provider_able_to_snapshot and not is_snapshot_behavior_specified_by_user:
        should_snapshot = True

    # create the snapshot, if necessary
    if should_snapshot and not source_location.host.is_local:
        snapshot_name = _snapshot_source_agent(source_location, mng_ctx)

    return snapshot_name


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
    initial_message: str | None,
    resume_message: str | None,
    source_location: HostLocation,
    mng_ctx: MngContext,
) -> CreateAgentOptions:
    # Get agent name from positional argument or --name flag, otherwise auto-generate
    parsed_agent_name: AgentName
    if opts.positional_name:
        parsed_agent_name = AgentName(opts.positional_name)
    elif opts.name:
        parsed_agent_name = AgentName(opts.name)
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
        # When creating a new remote host (--in/--new-host), always use COPY
        # since WORKTREE only works when source and target are on the same host
        is_creating_remote_host = opts.new_host is not None and opts.new_host.lower() != LOCAL_PROVIDER_NAME
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

    # Parse git options
    # new_branch: None = no new branch, "" = auto-generate name, "name" = use specified name
    is_new_branch = opts.new_branch is not None

    # --worktree requires a new branch; error if --no-new-branch is used with --worktree
    if copy_mode == WorkDirCopyMode.WORKTREE and not is_new_branch:
        raise UserInputError("--worktree requires a new branch. Cannot use --no-new-branch with --worktree.")

    new_branch = opts.new_branch

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
            base_branch=opts.base_branch or _get_current_git_branch(source_location, mng_ctx),
            is_new_branch=is_new_branch,
            new_branch_name=new_branch if new_branch else None,
            new_branch_prefix=opts.new_branch_prefix,
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
    env_vars = resolve_env_vars(opts.pass_agent_env, opts.agent_env)
    env_files = tuple(Path(f) for f in opts.agent_env_file)

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
        create_directories=tuple(Path(d) for d in opts.create_directory),
    )

    # Parse target_path if provided
    parsed_target_path = Path(opts.target_path) if opts.target_path else None

    # Determine if we should copy work_dir before building (default: copy if --no-connect)
    should_copy = opts.copy_work_dir
    if should_copy is None:
        should_copy = not opts.connect

    # Determine agent type: --agent-type takes priority, then positional argument
    # However, click may incorrectly assign values after -- to positional_agent_type
    # instead of agent_args. We detect this by checking if the value appears after
    # -- in sys.argv and move it to agent_args if so.
    #
    # Special case: --agent-cmd implies using the "generic" agent type, which simply
    # runs the provided command. If --agent-type is also specified to something other
    # than "generic", that's an error (they are mutually exclusive).
    resolved_agent_type = opts.agent_type
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
            # --agent-type was already specified, ignore the positional (could warn here)
            pass

    # Handle --agent-cmd: it implies using the "generic" agent type
    if opts.agent_command:
        if resolved_agent_type is not None and resolved_agent_type != "generic":
            raise UserInputError(
                f"--agent-cmd and --agent-type are mutually exclusive. "
                f"Use --agent-cmd to run a literal command (implicitly uses 'generic' agent type), "
                f"or use --agent-type to specify an agent type like '{resolved_agent_type}'."
            )
        # Automatically use the "generic" agent type when --agent-cmd is provided
        resolved_agent_type = "generic"

    agent_opts = CreateAgentOptions(
        agent_type=AgentTypeName(resolved_agent_type) if resolved_agent_type else None,
        name=parsed_agent_name,
        command=CommandString(opts.agent_command) if opts.agent_command else None,
        additional_commands=tuple(NamedCommand.from_string(c) for c in opts.add_command),
        agent_args=resolved_agent_args,
        user=opts.user,
        target_path=parsed_target_path,
        is_copy_immediate=should_copy,
        initial_message=initial_message,
        resume_message=resume_message,
        ready_timeout_seconds=opts.ready_timeout,
        data_options=data_options,
        git=git,
        environment=environment,
        lifecycle=lifecycle,
        permissions=permissions,
        label_options=label_options,
        provisioning=provisioning,
    )
    return agent_opts


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
    project_name: str | None,
    agent_and_host_loader: Callable[[], dict[HostReference, list[AgentReference]]],
    lifecycle: HostLifecycleOptions,
) -> HostReference | NewHostOptions | None:
    parsed_target_host: HostReference | NewHostOptions | None
    if opts.host:
        # Targeting an existing host
        agents_by_host = agent_and_host_loader()
        all_hosts = list(agents_by_host.keys())
        try:
            host_id = HostId(opts.host)
            host_ref = get_host_from_list_by_id(host_id, all_hosts)
        except ValueError:
            host_name = HostName(opts.host)
            host_ref = get_unique_host_from_list_by_name(host_name, all_hosts)
        if host_ref is None:
            raise UserInputError(f"Could not find host with ID or name: {opts.host}")
        parsed_target_host = host_ref
    elif opts.new_host:
        # Creating a new host
        # Parse host-level tags
        tags_dict: dict[str, str] = {}
        for tag_string in opts.tag:
            if "=" not in tag_string:
                raise UserInputError(f"Tag must be in KEY=VALUE format, got: {tag_string}")
            key, value = tag_string.split("=", 1)
            tags_dict[key.strip()] = value.strip()

        tags = tags_dict

        # Parse host environment
        host_env_vars = resolve_env_vars(opts.pass_host_env, opts.host_env)
        host_env_files = tuple(Path(f) for f in opts.host_env_file)

        # Combine build args from both individual (-b) and bulk (--build-args) options
        combined_build_args = _split_cli_args(opts.build_arg)
        if opts.build_args:
            combined_build_args = shlex.split(opts.build_args) + combined_build_args

        # Combine start args from both individual (-s) and bulk (--start-args) options
        combined_start_args = _split_cli_args(opts.start_arg)
        if opts.start_args:
            combined_start_args.extend(shlex.split(opts.start_args))

        # Parse build options
        build_options = NewHostBuildOptions(
            snapshot=SnapshotName(opts.snapshot) if opts.snapshot else None,
            context_path=Path(opts.project_context_path) if opts.project_context_path else None,
            build_args=tuple(combined_build_args),
            start_args=tuple(combined_start_args),
        )

        parsed_host_name_style = HostNameStyle(opts.host_name_style.upper())
        parsed_target_host = NewHostOptions(
            provider=ProviderInstanceName(opts.new_host),
            name=HostName(opts.host_name) if opts.host_name else None,
            name_style=parsed_host_name_style,
            tags=tags,
            build=build_options,
            environment=HostEnvironmentOptions(
                env_vars=host_env_vars,
                env_files=host_env_files,
                known_hosts=opts.known_hosts,
                authorized_keys=opts.authorized_keys,
            ),
            lifecycle=lifecycle,
        )
    else:
        # Default: local host
        parsed_target_host = None
    return parsed_target_host


# === Parsing Functions ===


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


def _snapshot_source_agent(location: HostLocation, mng_ctx: MngContext) -> str:
    """Snapshot the source agent before cloning."""
    raise NotImplementedError("_snapshot_source_agent not yet implemented")


def _assemble_result(
    agent_id: AgentId,
    host_id: HostId,
) -> tuple[AgentId, HostId]:
    """Assemble the result for output."""
    return (agent_id, host_id)


def _await_agent_stopped(
    agent: AgentInterface,
    poll_interval_seconds: float = 0.1,
    timeout_seconds: float = 300.0,
) -> None:
    """Wait for an agent to completely finish running.

    Polls the agent's is_running() status until it returns False.
    This is useful for scripting and testing when you need to wait
    for the agent to exit before proceeding.
    """
    logger.info("Waiting for agent to stop...")
    try:
        wait_for(
            condition=lambda: not agent.is_running(),
            timeout=timeout_seconds,
            poll_interval=poll_interval_seconds,
            error_message=f"Timeout waiting for agent {agent.name} to stop after {timeout_seconds} seconds",
        )
        logger.debug("Stopped agent {}", agent.name)
    except TimeoutError as e:
        raise click.ClickException(str(e)) from e


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
    synopsis="""mng [create|c] [<AGENT_NAME>] [<AGENT_TYPE>] [-t <TEMPLATE>] [--in <PROVIDER>] [--host <HOST>] [--c WINDOW_NAME=COMMAND]
    [--label KEY=VALUE] [--tag KEY=VALUE] [--project <PROJECT>] [--from <SOURCE>] [--in-place|--copy|--clone|--worktree]
    [--[no-]rsync] [--rsync-args <ARGS>] [--base-branch <BRANCH>] [--new-branch [<BRANCH-NAME>]] [--[no-]ensure-clean]
    [--snapshot <ID>] [-b <BUILD_ARG>] [-s <START_ARG>]
    [--env <KEY=VALUE>] [--env-file <FILE>] [--grant <PERMISSION>] [--user-command <COMMAND>] [--upload-file <LOCAL:REMOTE>]
    [--idle-timeout <SECONDS>] [--idle-mode <MODE>] [--start-on-boot|--no-start-on-boot] [--reuse|--no-reuse]
    [--[no-]auto-start] [--] [<AGENT_ARGS>...]""",
    aliases=("c",),
    arguments_description="""- `NAME`: Name for the agent (auto-generated if not provided)
- `AGENT_TYPE`: Which type of agent to run (default: `claude`). Can also be specified via `--agent-type`
- `AGENT_ARGS`: Additional arguments passed to the agent""",
    description="""This command sets up an agent's working directory, optionally provisions a
new host (or uses an existing one), runs the specified agent process, and
connects to it by default.

By default, agents run locally in a new git worktree (for git repositories)
or a copy of the current directory. Use --in to create a new remote host,
or --host to use an existing host.

The agent type defaults to 'claude' if not specified. Any command in your
PATH can also be used as an agent type. Arguments after -- are passed
directly to the agent command.

For local agents, mng creates a git worktree that shares objects with your
original repository, allowing efficient branch management. For remote agents,
the working directory is copied to the remote host.""",
    examples=(
        ("Create an agent locally in a new git worktree (default)", "mng create my-agent"),
        ("Create an agent in a Docker container", "mng create my-agent --in docker"),
        ("Create an agent in a Modal sandbox", "mng create my-agent --in modal"),
        ("Create using a named template", "mng create my-agent --template modal"),
        ("Stack multiple templates", "mng create my-agent -t modal -t codex"),
        ("Create a codex agent instead of claude", "mng create my-agent codex"),
        ("Pass arguments to the agent", "mng create my-agent -- --model opus"),
        ("Create on an existing host", "mng create my-agent --host my-dev-box"),
        ("Clone from an existing agent", "mng create new-agent --source other-agent"),
        ("Run directly in-place (no worktree)", "mng create my-agent --in-place"),
        ("Create without connecting", "mng create my-agent --no-connect"),
        ("Add extra tmux windows", 'mng create my-agent -c server="npm run dev"'),
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
            'By default, `mng create` uses the "local" host. Use these options to change that behavior.',
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
