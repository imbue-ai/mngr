from pathlib import Path

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mng.api.pull import pull_files
from imbue.mng.api.pull import pull_git
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


class PullCliOptions(CommonCliOptions):
    """Options passed from the CLI to the pull command.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.
    """

    source_pos: str | None
    destination_pos: str | None
    source: str | None
    source_agent: str | None
    source_host: str | None
    source_path: str | None
    destination: str | None
    dry_run: bool
    stop: bool
    delete: bool
    sync_mode: str
    exclude: tuple[str, ...]
    uncommitted_changes: str
    target_branch: str | None
    # Planned features (not yet implemented)
    target: str | None
    target_agent: str | None
    target_host: str | None
    target_path: str | None
    stdin: bool
    include: tuple[str, ...]
    include_gitignored: bool
    include_file: str | None
    exclude_file: str | None
    rsync_arg: tuple[str, ...]
    rsync_args: str | None
    branch: tuple[str, ...]
    all_branches: bool
    tags: bool
    force_git: bool
    merge: bool
    rebase: bool
    uncommitted_source: str | None


@click.command()
@click.argument("source_pos", default=None, required=False, metavar="SOURCE")
@click.argument("destination_pos", default=None, required=False, metavar="DESTINATION")
@optgroup.group("Source Selection")
@optgroup.option("--source", "source", help="Source specification: AGENT, AGENT:PATH, or PATH")
@optgroup.option("--source-agent", help="Source agent name or ID")
@optgroup.option("--source-host", help="Source host name or ID [future]")
@optgroup.option("--source-path", help="Path within the agent's work directory")
@optgroup.group("Destination")
@optgroup.option(
    "--destination",
    "destination",
    type=click.Path(),
    help="Local destination directory [default: .]",
)
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
    help="Stop the agent after pulling (for state consistency)",
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
    help="What to sync: files (working directory via rsync), git (merge git branches), or full (everything) [future]",
)
@optgroup.option(
    "--exclude",
    multiple=True,
    help="Patterns to exclude from sync [repeatable] [future]",
)
@optgroup.group("Target (for agent-to-agent sync)")
@optgroup.option(
    "--target",
    help="Target specification: AGENT, AGENT.HOST, AGENT.HOST:PATH, or HOST:PATH [future]",
)
@optgroup.option("--target-agent", help="Target agent name or ID [future]")
@optgroup.option("--target-host", help="Target host name or ID [future]")
@optgroup.option("--target-path", help="Path within target to sync to [future]")
@optgroup.group("Multi-source")
@optgroup.option(
    "--stdin",
    is_flag=True,
    help="Read source agents/hosts from stdin, one per line [future]",
)
@optgroup.group("File Filtering")
@optgroup.option(
    "--include",
    multiple=True,
    help="Include files matching glob pattern [repeatable] [future]",
)
@optgroup.option(
    "--include-gitignored",
    is_flag=True,
    help="Include files that match .gitignore patterns [future]",
)
@optgroup.option("--include-file", type=click.Path(), help="Read include patterns from file [future]")
@optgroup.option("--exclude-file", type=click.Path(), help="Read exclude patterns from file [future]")
@optgroup.group("Rsync Options")
@optgroup.option(
    "--rsync-arg",
    multiple=True,
    help="Additional argument to pass to rsync [repeatable] [future]",
)
@optgroup.option(
    "--rsync-args",
    help="Additional arguments to pass to rsync (as a single string) [future]",
)
@optgroup.group("Git Sync Options")
@optgroup.option("--branch", multiple=True, help="Pull a specific branch [repeatable] [future]")
@optgroup.option("--target-branch", help="Branch to merge into (git mode only) [default: current branch]")
@optgroup.option("--all-branches", "--all", is_flag=True, help="Pull all remote branches [future]")
@optgroup.option("--tags", is_flag=True, help="Include git tags in sync [future]")
@optgroup.option(
    "--force-git",
    is_flag=True,
    help="Force overwrite local git state (use with caution) [future]. Without this flag, the command fails if local and remote history have diverged (e.g. after a force-push) and the user must resolve manually.",
)
@optgroup.option("--merge", is_flag=True, help="Merge remote changes with local changes [future]")
@optgroup.option("--rebase", is_flag=True, help="Rebase local changes onto remote changes [future]")
@optgroup.option(
    "--uncommitted-source",
    type=click.Choice(["warn", "error"], case_sensitive=False),
    help="Warn or error if source has uncommitted changes [future]",
)
@optgroup.option(
    "--uncommitted-changes",
    type=click.Choice(["stash", "clobber", "merge", "fail"], case_sensitive=False),
    default="fail",
    show_default=True,
    help="How to handle uncommitted changes in the destination: stash (stash and leave stashed), clobber (overwrite), merge (stash, pull, unstash), fail (error if changes exist)",
)
@add_common_options
@click.pass_context
def pull(ctx: click.Context, **kwargs) -> None:
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="pull",
        command_class=PullCliOptions,
    )

    # Merge positional and named arguments (named option takes precedence)
    effective_source = opts.source if opts.source is not None else opts.source_pos
    effective_destination = opts.destination if opts.destination is not None else opts.destination_pos

    # Check for unsupported options
    if opts.sync_mode == "full":
        raise NotImplementedError("--sync-mode=full is not implemented yet")

    if opts.exclude:
        raise NotImplementedError("--exclude is not implemented yet")

    if opts.source_host is not None:
        raise NotImplementedError("--source-host is not implemented yet (only local agents are supported)")

    # Validate git-specific options
    if opts.target_branch is not None and opts.sync_mode != "git":
        raise UserInputError("--target-branch can only be used with --sync-mode=git")

    # Planned features - target options (for agent-to-agent sync)
    if opts.target is not None:
        raise NotImplementedError("--target is not implemented yet (agent-to-agent sync is planned)")
    if opts.target_agent is not None:
        raise NotImplementedError("--target-agent is not implemented yet (agent-to-agent sync is planned)")
    if opts.target_host is not None:
        raise NotImplementedError("--target-host is not implemented yet (agent-to-agent sync is planned)")
    if opts.target_path is not None:
        raise NotImplementedError("--target-path is not implemented yet (agent-to-agent sync is planned)")

    # Planned features - multi-source
    if opts.stdin:
        raise NotImplementedError("--stdin is not implemented yet")

    # Planned features - file filtering
    if opts.include:
        raise NotImplementedError("--include is not implemented yet")
    if opts.include_gitignored:
        raise NotImplementedError("--include-gitignored is not implemented yet")
    if opts.include_file is not None:
        raise NotImplementedError("--include-file is not implemented yet")
    if opts.exclude_file is not None:
        raise NotImplementedError("--exclude-file is not implemented yet")

    # Planned features - rsync options
    if opts.rsync_arg:
        raise NotImplementedError("--rsync-arg is not implemented yet")
    if opts.rsync_args is not None:
        raise NotImplementedError("--rsync-args is not implemented yet")

    # Planned features - git sync options (except --target-branch which is implemented)
    if opts.branch:
        raise NotImplementedError("--branch is not implemented yet")
    if opts.all_branches:
        raise NotImplementedError("--all-branches is not implemented yet")
    if opts.tags:
        raise NotImplementedError("--tags is not implemented yet")
    if opts.force_git:
        raise NotImplementedError("--force-git is not implemented yet")
    if opts.merge:
        raise NotImplementedError("--merge is not implemented yet")
    if opts.rebase:
        raise NotImplementedError("--rebase is not implemented yet")
    if opts.uncommitted_source is not None:
        raise NotImplementedError("--uncommitted-source is not implemented yet")

    # Parse source specification
    agent_identifier, source_path = parse_agent_spec(
        spec=effective_source,
        explicit_agent=opts.source_agent,
        spec_name="Source",
        default_subpath=opts.source_path,
    )

    # Determine destination
    destination_path = Path(effective_destination) if effective_destination else Path.cwd()

    # Find the agent
    result = find_agent_for_command(
        mng_ctx=mng_ctx,
        agent_identifier=agent_identifier,
        command_usage="pull <agent-id> <path>",
        host_filter=None,
    )
    if result is None:
        logger.info("No agent selected")
        return
    agent, host = result

    emit_info(f"Pulling from agent: {agent.name}", output_opts.output_format)

    # Parse uncommitted changes mode
    uncommitted_changes_mode = UncommittedChangesMode(opts.uncommitted_changes.upper())

    if opts.sync_mode == "git":
        if source_path is not None:
            raise UserInputError(
                "--sync-mode=git operates on the entire repository; "
                "subpath specifications (AGENT:PATH or --source-path) are not supported in git mode"
            )

        # Git mode: merge branches
        # source_branch=None means use agent's current branch
        git_result = pull_git(
            agent=agent,
            host=host,
            destination=destination_path,
            source_branch=None,
            target_branch=opts.target_branch,
            is_dry_run=opts.dry_run,
            uncommitted_changes=uncommitted_changes_mode,
            cg=mng_ctx.concurrency_group,
        )

        output_sync_git_result(git_result, output_opts.output_format)

        # Stop agent if requested (after outputting result so it's not lost if stop fails)
        if opts.stop:
            stop_agent_after_sync(agent, host, opts.dry_run, output_opts.output_format)
    else:
        # Files mode: rsync
        # Parse source_path if provided
        parsed_source_path: Path | None = None
        if source_path is not None:
            # If source_path is relative, make it relative to agent's work_dir
            parsed_path = Path(source_path)
            if parsed_path.is_absolute():
                parsed_source_path = parsed_path
            else:
                parsed_source_path = agent.work_dir / parsed_path

        files_result = pull_files(
            agent=agent,
            host=host,
            destination=destination_path,
            source_path=parsed_source_path,
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
    key="pull",
    one_line_description="Pull files or git commits from an agent to local machine [experimental]",
    synopsis="mng pull [SOURCE] [DESTINATION] [--source-agent <AGENT>] [--dry-run] [--stop]",
    description="""Syncs files or git state from an agent's working directory to a local directory.
Default behavior uses rsync for efficient incremental file transfer.
Use --sync-mode=git to merge git branches instead of syncing files.

If no source is specified, shows an interactive selector to choose an agent.""",
    examples=(
        ("Pull from agent to current directory", "mng pull my-agent"),
        ("Pull to specific local directory", "mng pull my-agent ./local-copy"),
        ("Pull specific subdirectory", "mng pull my-agent:src ./local-src"),
        ("Preview what would be transferred", "mng pull my-agent --dry-run"),
        ("Pull git commits", "mng pull my-agent --sync-mode=git"),
    ),
    additional_sections=(
        (
            "Multi-target Behavior",
            "See [multi_target](../generic/multi_target.md) for options controlling behavior "
            "when some agents cannot be processed.",
        ),
    ),
    see_also=(
        ("create", "Create a new agent"),
        ("list", "List agents to find one to pull from"),
        ("connect", "Connect to an agent interactively"),
        ("push", "Push files or git commits to an agent"),
    ),
).register()

# Add pager-enabled help option to the pull command
add_pager_help_option(pull)
