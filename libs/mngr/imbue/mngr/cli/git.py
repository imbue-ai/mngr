from pathlib import Path
from typing import Any

import click
from click_option_group import optgroup

from imbue.mngr.api.find import resolve_host_location
from imbue.mngr.api.git import git_pull
from imbue.mngr.api.git import git_push
from imbue.mngr.cli.address_params import HOST_LOCATION_ADDRESS
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.default_command_group import DefaultCommandGroup
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import emit_info
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.primitives import HostLocationAddress


class GitPushCliOptions(CommonCliOptions):
    """Options for ``mngr git push``."""

    target: HostLocationAddress
    start: bool
    git_args: tuple[str, ...]


class GitPullCliOptions(CommonCliOptions):
    """Options for ``mngr git pull``."""

    source: HostLocationAddress
    start: bool
    git_args: tuple[str, ...]


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

    resolved = resolve_host_location(parsed, mngr_ctx, is_start_desired=is_start_desired)
    return resolved.location


@git_command.command(
    name="push",
    context_settings={"ignore_unknown_options": True},
)
@click.argument("target", type=HOST_LOCATION_ADDRESS, metavar="TARGET")
@click.argument("git_args", nargs=-1, type=click.UNPROCESSED, metavar="[-- GIT_ARGS...]")
@optgroup.group("Sync Options")
@optgroup.option(
    "--start/--no-start",
    default=True,
    show_default=True,
    help="Automatically start the host if offline (the agent does not need to be running)",
)
@add_common_options
@click.pass_context
def git_push_command(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="git_push",
        command_class=GitPushCliOptions,
    )

    location = _resolve_remote_endpoint(opts.target, mngr_ctx, is_start_desired=opts.start)

    emit_info(f"Pushing to {location.path} on host {location.host.id}", output_opts.output_format)

    git_push(
        local_path=Path.cwd(),
        remote_host=location.host,
        remote_path=location.path,
        extra_args=opts.git_args,
        cg=mngr_ctx.concurrency_group,
        run_in_terminal=True,
    )


@git_command.command(
    name="pull",
    context_settings={"ignore_unknown_options": True},
)
@click.argument("source", type=HOST_LOCATION_ADDRESS, metavar="SOURCE")
@click.argument("git_args", nargs=-1, type=click.UNPROCESSED, metavar="[-- GIT_ARGS...]")
@optgroup.group("Sync Options")
@optgroup.option(
    "--start/--no-start",
    default=True,
    show_default=True,
    help="Automatically start the host if offline (the agent does not need to be running)",
)
@add_common_options
@click.pass_context
def git_pull_command(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="git_pull",
        command_class=GitPullCliOptions,
    )

    location = _resolve_remote_endpoint(opts.source, mngr_ctx, is_start_desired=opts.start)

    emit_info(f"Pulling from {location.path} on host {location.host.id}", output_opts.output_format)

    git_pull(
        local_path=Path.cwd(),
        remote_host=location.host,
        remote_path=location.path,
        extra_args=opts.git_args,
        cg=mngr_ctx.concurrency_group,
        run_in_terminal=True,
    )


CommandHelpMetadata(
    key="git",
    one_line_description="Push or pull git commits between local and a remote host or agent",
    synopsis="mngr git push|pull TARGET [-- GIT_ARGS...]",
    description="""Subcommand group for git-mediated synchronization with agents and remote hosts.

Each subcommand takes a single host-location address identifying the remote
endpoint (the local side is the current working directory). The address must
include an agent or a host -- bare local paths are rejected (use plain
``git push``/``git pull`` for local-only operations).

These are thin wrappers around ``git push`` / ``git pull``. mngr builds the
SSH URL and credentials for you, then runs the underlying git command;
anything you put after ``--`` is passed through verbatim. There's no
``--source-branch``, ``--target-branch``, ``--mirror``, or ``--dry-run`` --
use the corresponding git flags (``--force``, ``--tags``, refspec syntax,
``--dry-run``, ``--rebase`` etc.) directly.""",
    examples=(
        ("Push the current branch to an agent", "mngr git push my-agent"),
        ("Push with a refspec", "mngr git push my-agent -- feature:main"),
        ("Pull the agent's branch into the current working directory", "mngr git pull my-agent"),
        ("Pull a specific branch with rebase", "mngr git pull my-agent -- feature --rebase"),
    ),
    see_also=(
        ("rsync", "Rsync files between local and a remote host or agent"),
        ("pair", "Continuously sync files between agent and local"),
    ),
).register()


CommandHelpMetadata(
    key="git.push",
    one_line_description="Push git commits from the local repository to a remote agent or host",
    synopsis="mngr git push TARGET [--start/--no-start] [-- GIT_ARGS...]",
    description="""Push git commits from the current working directory's repository to a remote
agent or host's repository.

TARGET is a host-location address: ``AGENT[@HOST[.PROVIDER]][:PATH]`` or
``@HOST[.PROVIDER]:PATH``. A bare path is rejected (use plain ``git push``).

The local side is always the current working directory. mngr sets the
destination's ``receive.denyCurrentBranch=updateInstead`` and configures the
SSH transport, then runs ``git push <URL> <GIT_ARGS...>``. Any flags or
refspecs you supply after ``--`` are passed verbatim to the underlying
``git push``.""",
    examples=(
        ("Push the current branch to an agent", "mngr git push my-agent"),
        ("Push a specific branch with a refspec", "mngr git push my-agent -- feature:main"),
        ("Force-push all branches", "mngr git push my-agent -- --force --all"),
        ("Push to a path on a specific host", "mngr git push @host.modal:/work"),
        ("Preview what would be transferred", "mngr git push my-agent -- --dry-run"),
    ),
    see_also=(
        ("git pull", "Pull git commits from a remote repository to local"),
        ("rsync", "Rsync files between local and a remote host or agent"),
    ),
).register()


CommandHelpMetadata(
    key="git.pull",
    one_line_description="Pull git commits from a remote agent or host into the local repository",
    synopsis="mngr git pull SOURCE [--start/--no-start] [-- GIT_ARGS...]",
    description="""Pull git commits from a remote agent or host's repository into the current
working directory's repository.

SOURCE is a host-location address: ``AGENT[@HOST[.PROVIDER]][:PATH]`` or
``@HOST[.PROVIDER]:PATH``. A bare path is rejected (use plain ``git pull``).

The local side is always the current working directory. mngr configures the
SSH transport, then runs ``git pull <URL> <GIT_ARGS...>``. Any flags or
branch names you supply after ``--`` are passed verbatim to the underlying
``git pull``.""",
    examples=(
        ("Pull the current branch from an agent", "mngr git pull my-agent"),
        ("Pull a specific branch", "mngr git pull my-agent -- feature"),
        ("Rebase local changes onto agent's branch", "mngr git pull my-agent -- feature --rebase"),
        ("Pull from a path on a specific host", "mngr git pull @host.modal:/work"),
        ("Preview what would be merged", "mngr git pull my-agent -- --dry-run"),
    ),
    see_also=(
        ("git push", "Push git commits from local to a remote repository"),
        ("rsync", "Rsync files between local and a remote host or agent"),
    ),
).register()


add_pager_help_option(git_command)
add_pager_help_option(git_push_command)
add_pager_help_option(git_pull_command)
