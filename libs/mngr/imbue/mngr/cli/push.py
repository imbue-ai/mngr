from pathlib import Path

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import resolve_hosted_location
from imbue.mngr.api.push import push_files
from imbue.mngr.api.push import push_git
from imbue.mngr.cli.address_params import HOSTED_LOCATION
from imbue.mngr.cli.agent_utils import find_agent_for_command
from imbue.mngr.cli.agent_utils import stop_agent_after_sync
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import emit_info
from imbue.mngr.cli.output_helpers import output_sync_files_result
from imbue.mngr.cli.output_helpers import output_sync_git_result
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import HostedLocation
from imbue.mngr.primitives import UncommittedChangesMode


class PushCliOptions(CommonCliOptions):
    """Options passed from the CLI to the push command.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.
    """

    target_pos: HostedLocation | None
    source_pos: str | None
    target: HostedLocation | None
    source: str | None
    dry_run: bool
    stop: bool
    delete: bool
    sync_mode: str
    exclude: tuple[str, ...]
    uncommitted_changes: str
    source_branch: str | None
    mirror: bool
    rsync_only: bool


@click.command()
@click.argument("target_pos", type=HOSTED_LOCATION, default=None, required=False, metavar="TARGET")
@click.argument("source_pos", default=None, required=False, metavar="SOURCE")
@optgroup.group("Target Selection")
@optgroup.option(
    "--target",
    "target",
    type=HOSTED_LOCATION,
    help="Target specification: AGENT[@HOST[.PROVIDER]][:PATH]",
)
@optgroup.group("Source")
@optgroup.option("--source", "source", type=click.Path(exists=True), help="Local source directory [default: .]")
@optgroup.group("Sync Options")
@optgroup.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be transferred without actually transferring",
)
@optgroup.option(
    "--stop",
    is_flag=True,
    default=False,
    help="Stop the agent after pushing (for state consistency)",
)
@optgroup.option(
    "--delete/--no-delete",
    default=False,
    help="Delete files in destination that don't exist in source",
)
@optgroup.option(
    "--sync-mode",
    type=click.Choice(["files", "git", "full"], case_sensitive=False),
    default="files",
    show_default=True,
    help="What to sync: files (working directory via rsync), git (push git branches), or full (everything) [future]",
)
@optgroup.option(
    "--exclude",
    multiple=True,
    help="Patterns to exclude from sync [repeatable] [future]",
)
@optgroup.option(
    "--source-branch",
    help="Branch to push from (git mode only) [default: current branch]",
)
@optgroup.option(
    "--uncommitted-changes",
    type=click.Choice(["stash", "clobber", "merge", "fail"], case_sensitive=False),
    default="fail",
    show_default=True,
    help="How to handle uncommitted changes in the agent workspace: stash (stash and leave stashed), clobber (overwrite), merge (stash, push, unstash), fail (error if changes exist)",
)
@optgroup.group("Git Options")
@optgroup.option(
    "--mirror",
    is_flag=True,
    default=False,
    help="Force the agent's git state to match the source, overwriting all refs (branches, tags) and resetting the working tree (dangerous). Any commits or branches that exist only in the agent will be lost. Only applies to --sync-mode=git. Required when the agent and source have diverged (non-fast-forward). For remote agents, pushes all local branches and tags [future].",
)
@optgroup.option(
    "--rsync-only",
    is_flag=True,
    default=False,
    help="Use rsync even if git is available in both source and destination",
)
@add_common_options
@click.pass_context
def push(ctx: click.Context, **kwargs) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="push",
        command_class=PushCliOptions,
    )

    if opts.target is not None and opts.target_pos is not None and opts.target != opts.target_pos:
        raise UserInputError("Cannot specify both TARGET and --target with different values")
    effective_target_loc: HostedLocation | None = opts.target if opts.target is not None else opts.target_pos
    effective_source = opts.source if opts.source is not None else opts.source_pos

    # Check for unsupported options
    if opts.sync_mode == "full":
        raise NotImplementedError("--sync-mode=full is not implemented yet")

    if opts.exclude:
        raise NotImplementedError("--exclude is not implemented yet")

    # Validate git-specific options
    if opts.source_branch is not None and opts.sync_mode != "git":
        raise UserInputError("--source-branch can only be used with --sync-mode=git")

    if opts.mirror and opts.sync_mode != "git":
        raise UserInputError("--mirror can only be used with --sync-mode=git")

    if opts.rsync_only:
        if opts.source_branch is not None:
            raise UserInputError("--source-branch has no effect with --rsync-only")
        if opts.mirror:
            raise UserInputError("--mirror has no effect with --rsync-only")
        if opts.sync_mode == "git":
            raise NotImplementedError(
                "--rsync-only with --sync-mode=git is not yet supported; use --sync-mode=files instead"
            )

    # Determine source path (the local side)
    source_path = Path(effective_source) if effective_source else Path.cwd()

    agent: AgentInterface | None
    host: OnlineHostInterface
    target_remote_path: Path
    if (
        effective_target_loc is not None
        and effective_target_loc.agent is None
        and effective_target_loc.host is not None
    ):
        # @HOST:PATH target: resolve the host directly without picking an agent.
        # `resolve_hosted_location` enforces that path must be set when no agent
        # is given (no agent.work_dir to fall back to).
        agents_by_host, _ = discover_hosts_and_agents(
            mngr_ctx,
            provider_names=None,
            agent_identifiers=None,
            include_destroyed=False,
            reset_caches=False,
        )
        resolved = resolve_hosted_location(effective_target_loc, agents_by_host, mngr_ctx)
        agent = None
        host = resolved.location.host
        target_remote_path = resolved.location.path
        emit_info(f"Pushing to target: {target_remote_path}", output_opts.output_format)
    else:
        target_address: AgentAddress | None = None
        target_subpath: Path | None = None
        if effective_target_loc is not None:
            if effective_target_loc.agent is not None:
                target_address = AgentAddress(agent=effective_target_loc.agent, host=effective_target_loc.host)
            target_subpath = effective_target_loc.path

        result = find_agent_for_command(mngr_ctx=mngr_ctx, address=target_address)
        if result is None:
            logger.info("No agent selected")
            return
        agent, host = result

        target_remote_path = agent.work_dir
        if target_subpath is not None:
            if target_subpath.is_absolute():
                target_remote_path = target_subpath
            else:
                target_remote_path = agent.work_dir / target_subpath

        emit_info(f"Pushing to agent: {agent.name}", output_opts.output_format)

    if opts.stop and agent is None:
        raise UserInputError("--stop requires an agent (cannot stop a host-only target)")

    # Parse uncommitted changes mode
    uncommitted_changes_mode = UncommittedChangesMode(opts.uncommitted_changes.upper())

    if opts.sync_mode == "git" and not opts.rsync_only:
        # Git mode: push branches
        git_result = push_git(
            host=host,
            source=source_path,
            destination_path=target_remote_path,
            source_branch=opts.source_branch,
            target_branch=None,
            is_dry_run=opts.dry_run,
            uncommitted_changes=uncommitted_changes_mode,
            is_mirror=opts.mirror,
            cg=mngr_ctx.concurrency_group,
        )

        output_sync_git_result(git_result, output_opts.output_format)
    else:
        # Files mode: rsync
        files_result = push_files(
            host=host,
            source=source_path,
            destination_path=target_remote_path,
            is_dry_run=opts.dry_run,
            is_delete=opts.delete,
            uncommitted_changes=uncommitted_changes_mode,
            cg=mngr_ctx.concurrency_group,
        )

        output_sync_files_result(files_result, output_opts.output_format)

    # Stop agent if requested (after outputting result so it's not lost if stop fails)
    if opts.stop and agent is not None:
        stop_agent_after_sync(agent, host, opts.dry_run, output_opts.output_format)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="push",
    one_line_description="Push files or git commits from local machine to an agent [experimental]",
    synopsis="mngr push [TARGET] [SOURCE] [--target <TARGET>] [--source <DIR>] [--sync-mode <MODE>] [--mirror] [--dry-run] [--stop]",
    description="""Syncs files or git state from a local directory to an agent's working directory.
Default behavior uses rsync for efficient incremental file transfer.
Use --sync-mode=git to push git branches instead of syncing files.

If no target is specified, shows an interactive selector to choose an agent.

IMPORTANT: The source (host) workspace is never modified. Only the target
(agent workspace) may be modified.""",
    examples=(
        ("Push to agent from current directory", "mngr push my-agent"),
        ("Push from specific local directory", "mngr push my-agent ./local-dir"),
        ("Push to specific subdirectory", "mngr push my-agent:subdir ./local-src"),
        ("Push to a path on a host directly (no agent)", "mngr push @localhost:/abs/path ./local-dir"),
        ("Preview what would be transferred", "mngr push my-agent --dry-run"),
        ("Push git commits", "mngr push my-agent --sync-mode=git"),
        ("Mirror all refs to agent", "mngr push my-agent --sync-mode=git --mirror"),
    ),
    see_also=(
        ("create", "Create a new agent"),
        ("list", "List agents to find one to push to"),
        ("pull", "Pull files or git commits from an agent"),
        ("pair", "Continuously sync files between agent and local"),
    ),
).register()

add_pager_help_option(push)
