from pathlib import Path
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mng.cli.agent_utils import find_agent_for_command
from imbue.mng.cli.agent_utils import parse_agent_spec
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.output_helpers import emit_event
from imbue.mng.cli.output_helpers import emit_info
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.config.data_types import CommonCliOptions
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.errors import MngError
from imbue.mng.primitives import ConflictMode
from imbue.mng.primitives import OutputFormat
from imbue.mng.primitives import SyncDirection
from imbue.mng.primitives import UncommittedChangesMode
from imbue.mng.utils.git_utils import find_git_worktree_root
from imbue.mng_pair.api import pair_files


class PairCliOptions(CommonCliOptions):
    """Options passed from the CLI to the pair command."""

    source_pos: str | None
    source: str | None
    source_agent: str | None
    source_host: str | None
    source_path: str | None
    target: str | None
    require_git: bool
    sync_direction: str
    conflict: str
    uncommitted_changes: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]


def _emit_pair_started(
    source_path: Path,
    target_path: Path,
    output_opts: OutputOptions,
) -> None:
    """Emit a message when pairing starts."""
    data = {
        "source_path": str(source_path),
        "target_path": str(target_path),
    }
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("pair_started", data, output_opts.output_format)
        case OutputFormat.HUMAN:
            write_human_line("Pairing {} <-> {}", source_path, target_path)
        case _ as unreachable:
            assert_never(unreachable)


def _emit_pair_stopped(output_opts: OutputOptions) -> None:
    """Emit a message when pairing stops."""
    data: dict[str, str] = {}
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("pair_stopped", data, output_opts.output_format)
        case OutputFormat.HUMAN:
            write_human_line("Pairing stopped")
        case _ as unreachable:
            assert_never(unreachable)


@click.command()
@click.argument("source_pos", default=None, required=False, metavar="SOURCE")
@optgroup.group("Source Selection")
@optgroup.option("--source", "source", help="Source specification: AGENT, AGENT:PATH, or PATH")
@optgroup.option("--source-agent", help="Source agent name or ID")
@optgroup.option("--source-host", help="Source host name or ID")
@optgroup.option("--source-path", help="Path within the agent's work directory")
@optgroup.group("Target")
@optgroup.option(
    "--target",
    "target",
    type=click.Path(),
    help="Local target directory [default: nearest git root or current directory]",
)
@optgroup.group("Git Handling")
@optgroup.option(
    "--require-git/--no-require-git",
    default=True,
    help="Require that both source and target are git repositories [default: require git]",
)
@optgroup.option(
    "--uncommitted-changes",
    type=click.Choice(["stash", "clobber", "merge", "fail"], case_sensitive=False),
    default="fail",
    show_default=True,
    help="How to handle uncommitted changes during initial git sync. The initial sync aborts immediately if unresolved conflicts exist, regardless of this setting.",
)
@optgroup.group("Sync Behavior")
@optgroup.option(
    "--sync-direction",
    type=click.Choice(["both", "forward", "reverse"], case_sensitive=False),
    default="both",
    show_default=True,
    help="Sync direction: both (bidirectional), forward (source->target), reverse (target->source)",
)
@optgroup.option(
    "--conflict",
    type=click.Choice(["newer", "source", "target", "ask"], case_sensitive=False),
    default="newer",
    show_default=True,
    help="Conflict resolution mode (only matters for bidirectional sync). 'newer' prefers the file with the more recent modification time (uses unison's -prefer newer; note that clock skew between machines can cause incorrect results). 'source' and 'target' always prefer that side. 'ask' prompts interactively [future].",
)
@optgroup.group("File Filtering")
@optgroup.option(
    "--include",
    multiple=True,
    help="Include files matching glob pattern [repeatable]",
)
@optgroup.option(
    "--exclude",
    multiple=True,
    help="Exclude files matching glob pattern [repeatable]",
)
@add_common_options
@click.pass_context
def pair(ctx: click.Context, **kwargs) -> None:
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="pair",
        command_class=PairCliOptions,
    )

    # Merge positional and named arguments (named option takes precedence)
    effective_source = opts.source if opts.source is not None else opts.source_pos

    # Parse source specification
    agent_identifier, source_subpath = parse_agent_spec(
        spec=effective_source,
        explicit_agent=opts.source_agent,
        spec_name="Source",
        default_subpath=opts.source_path,
    )

    # Determine target path
    if opts.target is not None:
        target_path = Path(opts.target)
    else:
        # Default to nearest git root, or current directory
        git_root = find_git_worktree_root(None, mng_ctx.concurrency_group)
        target_path = git_root if git_root is not None else Path.cwd()

    # Find the agent
    result = find_agent_for_command(
        mng_ctx=mng_ctx,
        agent_identifier=agent_identifier,
        command_usage="pair <agent-id>",
        host_filter=opts.source_host,
    )
    if result is None:
        logger.info("No agent selected")
        return
    agent, host = result

    # Only local agents are supported right now
    if not host.is_local:
        raise NotImplementedError("Pairing with remote agents is not implemented yet")

    # Determine source path (agent's work_dir, potentially with subpath)
    source_path = agent.work_dir
    if source_subpath is not None:
        parsed_subpath = Path(source_subpath)
        if parsed_subpath.is_absolute():
            source_path = parsed_subpath
        else:
            source_path = agent.work_dir / parsed_subpath

    emit_info(f"Pairing with agent: {agent.name}", output_opts.output_format)

    # Parse enum options
    sync_direction = SyncDirection(opts.sync_direction.upper())
    conflict_mode = ConflictMode(opts.conflict.upper())
    uncommitted_changes_mode = UncommittedChangesMode(opts.uncommitted_changes.upper())

    _emit_pair_started(source_path, target_path, output_opts)

    # Start the pair sync
    try:
        with pair_files(
            agent=agent,
            host=host,
            agent_path=source_path,
            local_path=target_path,
            sync_direction=sync_direction,
            conflict_mode=conflict_mode,
            is_require_git=opts.require_git,
            uncommitted_changes=uncommitted_changes_mode,
            exclude_patterns=opts.exclude,
            include_patterns=opts.include,
            cg=mng_ctx.concurrency_group,
        ) as syncer:
            emit_info("Sync started. Press Ctrl+C to stop.", output_opts.output_format)

            # Wait for the syncer to complete (usually via Ctrl+C)
            exit_code = syncer.wait()
            if exit_code != 0:
                raise MngError(f"Unison exited with code {exit_code}")
    except KeyboardInterrupt:
        logger.debug("Received keyboard interrupt")
    finally:
        _emit_pair_stopped(output_opts)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="pair",
    one_line_description="Continuously sync files between an agent and local directory [experimental]",
    synopsis="mng pair [SOURCE] [--target <DIR>] [--sync-direction <DIR>] [--conflict <MODE>]",
    description="""This command establishes a bidirectional file sync between an agent's working
directory and a local directory. Changes are watched and synced in real-time.

If git repositories exist on both sides, the command first synchronizes git
state (branches and commits) before starting the continuous file sync.

Press Ctrl+C to stop the sync.

During rapid concurrent edits, changes will be debounced to avoid partial writes [future].""",
    examples=(
        ("Pair with an agent", "mng pair my-agent"),
        ("Pair to specific local directory", "mng pair my-agent --target ./local-dir"),
        ("One-way sync (source to target)", "mng pair my-agent --sync-direction=forward"),
        ("Prefer source on conflicts", "mng pair my-agent --conflict=source"),
        ("Filter to specific host", "mng pair my-agent --source-host @local"),
        ("Use --source-agent flag", "mng pair --source-agent my-agent --target ./local-copy"),
    ),
    see_also=(
        ("push", "Push files or git commits to an agent"),
        ("pull", "Pull files or git commits from an agent"),
        ("create", "Create a new agent"),
        ("list", "List agents to find one to pair with"),
    ),
).register()

add_pager_help_option(pair)
