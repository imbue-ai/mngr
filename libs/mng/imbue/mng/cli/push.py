from pathlib import Path

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mng.api.push import push_files
from imbue.mng.api.push import push_git
from imbue.mng.cli.agent_utils import find_agent_for_command
from imbue.mng.cli.agent_utils import parse_agent_spec
from imbue.mng.cli.agent_utils import stop_agent_after_sync
from imbue.mng.cli.common_opts import CommonCliOptions
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.output_helpers import emit_info
from imbue.mng.cli.output_helpers import output_sync_files_result
from imbue.mng.cli.output_helpers import output_sync_git_result
from imbue.mng.errors import UserInputError
from imbue.mng.primitives import UncommittedChangesMode


class PushCliOptions(CommonCliOptions):
    """Options passed from the CLI to the push command.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.
    """

    target_pos: str | None
    source_pos: str | None
    target: str | None
    target_agent: str | None
    target_host: str | None
    target_path: str | None
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
@click.argument("target_pos", default=None, required=False, metavar="TARGET")
@click.argument("source_pos", default=None, required=False, metavar="SOURCE")
@optgroup.group("Target Selection")
@optgroup.option("--target", "target", help="Target specification: AGENT, AGENT:PATH, or PATH")
@optgroup.option("--target-agent", help="Target agent name or ID")
@optgroup.option("--target-host", help="Target host name or ID [future]")
@optgroup.option("--target-path", help="Path within the agent's work directory")
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
    help="Force the agent's git state to match the source, overwriting all refs (branches, tags) and resetting the working tree (dangerous). Any commits or branches that exist only in the agent will be lost. Only applies to --sync-mode=git. Required when the agent and source have diverged (non-fast-forward). For remote agents, uses git push --mirror [future].",
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
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="push",
        command_class=PushCliOptions,
    )

    # Merge positional and named arguments (named option takes precedence)
    effective_target = opts.target if opts.target is not None else opts.target_pos
    effective_source = opts.source if opts.source is not None else opts.source_pos

    # Check for unsupported options
    if opts.sync_mode == "full":
        raise NotImplementedError("--sync-mode=full is not implemented yet")

    if opts.exclude:
        raise NotImplementedError("--exclude is not implemented yet")

    if opts.target_host is not None:
        raise NotImplementedError("--target-host is not implemented yet (only local agents are supported)")

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

    # Parse target specification
    agent_identifier, target_path = parse_agent_spec(
        spec=effective_target,
        explicit_agent=opts.target_agent,
        spec_name="Target",
        default_subpath=opts.target_path,
    )

    # Determine source path
    source_path = Path(effective_source) if effective_source else Path.cwd()

    # Find the agent
    result = find_agent_for_command(
        mng_ctx=mng_ctx,
        agent_identifier=agent_identifier,
        command_usage="push <agent-id> <path>",
        host_filter=None,
    )
    if result is None:
        logger.info("No agent selected")
        return
    agent, host = result

    emit_info(f"Pushing to agent: {agent.name}", output_opts.output_format)

    # Parse uncommitted changes mode
    uncommitted_changes_mode = UncommittedChangesMode(opts.uncommitted_changes.upper())

    if opts.sync_mode == "git" and not opts.rsync_only:
        if target_path is not None:
            raise UserInputError(
                "--sync-mode=git operates on the entire repository; "
                "subpath specifications (AGENT:PATH or --target-path) are not supported in git mode"
            )

        # Git mode: push branches
        git_result = push_git(
            agent=agent,
            host=host,
            source=source_path,
            source_branch=opts.source_branch,
            target_branch=None,
            is_dry_run=opts.dry_run,
            uncommitted_changes=uncommitted_changes_mode,
            is_mirror=opts.mirror,
            cg=mng_ctx.concurrency_group,
        )

        output_sync_git_result(git_result, output_opts.output_format)

        # Stop agent if requested (after outputting result so it's not lost if stop fails)
        if opts.stop:
            stop_agent_after_sync(agent, host, opts.dry_run, output_opts.output_format)
    else:
        # Files mode: rsync
        # Parse target_path if provided
        parsed_target_path: Path | None = None
        if target_path is not None:
            # If target_path is relative, make it relative to agent's work_dir
            parsed_path = Path(target_path)
            if parsed_path.is_absolute():
                parsed_target_path = parsed_path
            else:
                parsed_target_path = agent.work_dir / parsed_path

        files_result = push_files(
            agent=agent,
            host=host,
            source=source_path,
            destination_path=parsed_target_path,
            is_dry_run=opts.dry_run,
            is_delete=opts.delete,
            uncommitted_changes=uncommitted_changes_mode,
            cg=mng_ctx.concurrency_group,
        )

        output_sync_files_result(files_result, output_opts.output_format)

        # Stop agent if requested (after outputting result so it's not lost if stop fails)
        if opts.stop:
            stop_agent_after_sync(agent, host, opts.dry_run, output_opts.output_format)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="push",
    one_line_description="Push files or git commits from local machine to an agent [experimental]",
    synopsis="mng push [TARGET] [SOURCE] [--target-agent <AGENT>] [--dry-run] [--stop]",
    description="""Syncs files or git state from a local directory to an agent's working directory.
Default behavior uses rsync for efficient incremental file transfer.
Use --sync-mode=git to push git branches instead of syncing files.

If no target is specified, shows an interactive selector to choose an agent.

IMPORTANT: The source (host) workspace is never modified. Only the target
(agent workspace) may be modified.""",
    examples=(
        ("Push to agent from current directory", "mng push my-agent"),
        ("Push from specific local directory", "mng push my-agent ./local-dir"),
        ("Push to specific subdirectory", "mng push my-agent:subdir ./local-src"),
        ("Preview what would be transferred", "mng push my-agent --dry-run"),
        ("Push git commits", "mng push my-agent --sync-mode=git"),
        ("Mirror all refs to agent", "mng push my-agent --sync-mode=git --mirror"),
    ),
    see_also=(
        ("create", "Create a new agent"),
        ("list", "List agents to find one to push to"),
        ("pull", "Pull files or git commits from an agent"),
        ("pair", "Continuously sync files between agent and local"),
    ),
).register()

add_pager_help_option(push)
