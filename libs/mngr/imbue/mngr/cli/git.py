from pathlib import Path
from typing import Any

import click
from click_option_group import optgroup

from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import resolve_host_location_address
from imbue.mngr.api.sync import git_pull
from imbue.mngr.api.sync import git_push
from imbue.mngr.cli.address_params import HOST_LOCATION_ADDRESS
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.default_command_group import DefaultCommandGroup
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import emit_info
from imbue.mngr.cli.output_helpers import output_git_pull_result
from imbue.mngr.cli.output_helpers import output_git_push_result
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.primitives import HostLocationAddress
from imbue.mngr.primitives import UncommittedChangesMode


class GitPushCliOptions(CommonCliOptions):
    """Options for the ``mngr git push`` subcommand."""

    target: HostLocationAddress
    dry_run: bool
    start: bool
    source_branch: str | None
    target_branch: str | None
    uncommitted_changes: str
    mirror: bool
    # Future placeholders
    branch: tuple[str, ...]
    all_branches: bool
    tags: bool


class GitPullCliOptions(CommonCliOptions):
    """Options for the ``mngr git pull`` subcommand."""

    source: HostLocationAddress
    dry_run: bool
    start: bool
    source_branch: str | None
    target_branch: str | None
    uncommitted_changes: str
    # Future placeholders
    branch: tuple[str, ...]
    all_branches: bool
    tags: bool
    force_git: bool
    merge: bool
    rebase: bool
    uncommitted_source: str | None


class _GitGroup(DefaultCommandGroup):
    """Subcommand group for git push/pull operations against an agent or remote host."""

    _config_key = "git"


@click.group(name="git", cls=_GitGroup)
@add_common_options
@click.pass_context
def git_command(ctx: click.Context, **kwargs: Any) -> None:
    pass


def _resolve_remote_endpoint(
    parsed: HostLocationAddress,
    mngr_ctx: MngrContext,
    *,
    is_start_desired: bool,
) -> HostLocation:
    """Resolve a HostLocationAddress for a git push/pull endpoint.

    Rejects addresses that have no agent and no host (those are bare-local paths;
    use plain ``git push``/``git pull`` for local-only operations).
    """
    if parsed.agent is None and parsed.host is None:
        raise UserInputError(
            "git push/pull requires an agent or remote host -- use plain ``git push``/``git pull`` "
            "for local-only operations"
        )

    agents_by_host, _ = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=None,
        agent_identifiers=None,
        include_destroyed=False,
        reset_caches=False,
    )
    resolved = resolve_host_location_address(
        parsed,
        agents_by_host,
        mngr_ctx,
        is_start_desired=is_start_desired,
    )
    return resolved.location


@git_command.command(name="push")
@click.argument("target", type=HOST_LOCATION_ADDRESS, metavar="TARGET")
@optgroup.group("Sync Options")
@optgroup.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be transferred without actually transferring",
)
@optgroup.option(
    "--start/--no-start",
    default=True,
    show_default=True,
    help="Automatically start the host if offline (the agent does not need to be running)",
)
@optgroup.group("Git Options")
@optgroup.option(
    "--source-branch",
    help="Branch to push from [default: current branch on the local machine]",
)
@optgroup.option(
    "--target-branch",
    help="Branch to push to [default: current branch on the remote]",
)
@optgroup.option(
    "--mirror",
    is_flag=True,
    default=False,
    help=(
        "Force the remote's git state to match the source, overwriting all refs (branches, tags) "
        "and resetting the working tree (dangerous). Any commits or branches that exist only on "
        "the remote will be lost. Required when the remote and the source have diverged "
        "(non-fast-forward). For non-local hosts, pushes all local branches and tags."
    ),
)
@optgroup.option(
    "--uncommitted-changes",
    type=click.Choice(["stash", "clobber", "merge", "fail"], case_sensitive=False),
    default="fail",
    show_default=True,
    help=("How to handle uncommitted changes on the remote (the side being modified): stash, clobber, merge, or fail"),
)
@optgroup.option("--branch", multiple=True, help="Push specific branches [repeatable] [future]")
@optgroup.option("--all-branches", "--all", is_flag=True, help="Push all branches [future]")
@optgroup.option("--tags", is_flag=True, help="Include git tags in push [future]")
@add_common_options
@click.pass_context
def git_push_command(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="git_push",
        command_class=GitPushCliOptions,
    )

    if opts.branch:
        raise NotImplementedError("--branch is not implemented yet")
    if opts.all_branches:
        raise NotImplementedError("--all-branches is not implemented yet")
    if opts.tags:
        raise NotImplementedError("--tags is not implemented yet")

    location = _resolve_remote_endpoint(opts.target, mngr_ctx, is_start_desired=opts.start)

    uncommitted_changes_mode = UncommittedChangesMode(opts.uncommitted_changes.upper())

    emit_info(f"Pushing to {location.path} on host {location.host.id}", output_opts.output_format)

    result = git_push(
        local_path=Path.cwd(),
        remote_host=location.host,
        remote_path=location.path,
        source_branch=opts.source_branch,
        target_branch=opts.target_branch,
        is_dry_run=opts.dry_run,
        uncommitted_changes=uncommitted_changes_mode,
        is_mirror=opts.mirror,
        cg=mngr_ctx.concurrency_group,
    )

    output_git_push_result(result, output_opts.output_format)


@git_command.command(name="pull")
@click.argument("source", type=HOST_LOCATION_ADDRESS, metavar="SOURCE")
@optgroup.group("Sync Options")
@optgroup.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be transferred without actually transferring",
)
@optgroup.option(
    "--start/--no-start",
    default=True,
    show_default=True,
    help="Automatically start the host if offline (the agent does not need to be running)",
)
@optgroup.group("Git Options")
@optgroup.option(
    "--source-branch",
    help="Branch to pull from [default: current branch on the remote]",
)
@optgroup.option(
    "--target-branch",
    help="Branch to merge into on the local side [default: current branch locally]",
)
@optgroup.option(
    "--uncommitted-changes",
    type=click.Choice(["stash", "clobber", "merge", "fail"], case_sensitive=False),
    default="fail",
    show_default=True,
    help=("How to handle uncommitted changes locally (the side being modified): stash, clobber, merge, or fail"),
)
@optgroup.option("--branch", multiple=True, help="Pull specific branches [repeatable] [future]")
@optgroup.option("--all-branches", "--all", is_flag=True, help="Pull all remote branches [future]")
@optgroup.option("--tags", is_flag=True, help="Include git tags in sync [future]")
@optgroup.option(
    "--force-git",
    is_flag=True,
    help="Force overwrite local git state (use with caution) [future]",
)
@optgroup.option("--merge", is_flag=True, help="Merge remote changes with local changes [future]")
@optgroup.option("--rebase", is_flag=True, help="Rebase local changes onto remote changes [future]")
@optgroup.option(
    "--uncommitted-source",
    type=click.Choice(["warn", "error"], case_sensitive=False),
    help="Warn or error if the remote has uncommitted changes [future]",
)
@add_common_options
@click.pass_context
def git_pull_command(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="git_pull",
        command_class=GitPullCliOptions,
    )

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

    location = _resolve_remote_endpoint(opts.source, mngr_ctx, is_start_desired=opts.start)

    uncommitted_changes_mode = UncommittedChangesMode(opts.uncommitted_changes.upper())

    emit_info(f"Pulling from {location.path} on host {location.host.id}", output_opts.output_format)

    result = git_pull(
        local_path=Path.cwd(),
        remote_host=location.host,
        remote_path=location.path,
        source_branch=opts.source_branch,
        target_branch=opts.target_branch,
        is_dry_run=opts.dry_run,
        uncommitted_changes=uncommitted_changes_mode,
        cg=mngr_ctx.concurrency_group,
    )

    output_git_pull_result(result, output_opts.output_format)


# Help metadata for the group and its subcommands

CommandHelpMetadata(
    key="git",
    one_line_description="Push or pull git commits between local and a remote host or agent",
    synopsis="mngr git push|pull TARGET [OPTIONS]",
    description="""Subcommand group for git-mediated synchronization with agents and remote hosts.

Each subcommand takes a single host-location address identifying the remote
endpoint (the local side is the current working directory). The address must
include an agent or a host -- bare local paths are rejected (use plain
``git push``/``git pull`` for local-only operations).""",
    examples=(
        ("Push the current branch to an agent", "mngr git push my-agent"),
        ("Pull the agent's branch into the current working directory", "mngr git pull my-agent"),
    ),
    see_also=(
        ("rsync", "Rsync files between local and a remote host or agent"),
        ("pair", "Continuously sync files between agent and local"),
    ),
).register()


CommandHelpMetadata(
    key="git.push",
    one_line_description="Push git commits from the local repository to a remote agent or host",
    synopsis="mngr git push TARGET [--source-branch BRANCH] [--target-branch BRANCH] [--mirror] [--dry-run]",
    description="""Push git commits from the current working directory's repository to a remote
agent or host's repository.

TARGET is a host-location address: ``AGENT[@HOST[.PROVIDER]][:PATH]`` or
``@HOST[.PROVIDER]:PATH``. A bare path is rejected (use plain ``git push``).

The local side is always the current working directory.""",
    examples=(
        ("Push the current branch to an agent", "mngr git push my-agent"),
        ("Push a specific branch", "mngr git push my-agent --source-branch feature"),
        ("Force-overwrite the agent's refs", "mngr git push my-agent --mirror"),
        ("Push to a path on a specific host", "mngr git push @host.modal:/work"),
        ("Preview what would be transferred", "mngr git push my-agent --dry-run"),
    ),
    see_also=(
        ("git pull", "Pull git commits from a remote repository to local"),
        ("rsync", "Rsync files between local and a remote host or agent"),
    ),
).register()


CommandHelpMetadata(
    key="git.pull",
    one_line_description="Pull git commits from a remote agent or host into the local repository",
    synopsis="mngr git pull SOURCE [--source-branch BRANCH] [--target-branch BRANCH] [--dry-run]",
    description="""Pull git commits from a remote agent or host's repository into the current
working directory's repository (by fetching and merging).

SOURCE is a host-location address: ``AGENT[@HOST[.PROVIDER]][:PATH]`` or
``@HOST[.PROVIDER]:PATH``. A bare path is rejected (use plain ``git pull``).

The local side is always the current working directory.""",
    examples=(
        ("Pull the current branch from an agent", "mngr git pull my-agent"),
        ("Pull a specific branch", "mngr git pull my-agent --source-branch feature"),
        ("Pull from a path on a specific host", "mngr git pull @host.modal:/work"),
        ("Preview what would be merged", "mngr git pull my-agent --dry-run"),
    ),
    see_also=(
        ("git push", "Push git commits from local to a remote repository"),
        ("rsync", "Rsync files between local and a remote host or agent"),
    ),
).register()


add_pager_help_option(git_command)
add_pager_help_option(git_push_command)
add_pager_help_option(git_pull_command)
