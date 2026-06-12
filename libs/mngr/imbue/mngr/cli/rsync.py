from pathlib import Path
from typing import Any

import click
from click_option_group import optgroup

from imbue.mngr.api.find import resolve_host_location
from imbue.mngr.api.rsync import RsyncEndpointError
from imbue.mngr.api.rsync import rsync
from imbue.mngr.cli.address_params import HOST_LOCATION_ADDRESS
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import HostLocationAddress
from imbue.mngr.primitives import UncommittedChangesMode


class RsyncCliOptions(CommonCliOptions):
    """Options for the rsync command."""

    source: HostLocationAddress
    destination: HostLocationAddress
    start: bool
    uncommitted_changes: str
    include_gitignored: bool
    rsync_args: tuple[str, ...]


def _user_path_to_str(path: Path, has_trailing_slash: bool) -> str:
    """Format a user-supplied Path back into a string, preserving the trailing ``/``."""
    s = str(path)
    if has_trailing_slash and not s.endswith("/"):
        s += "/"
    return s


def _resolve_endpoint(
    parsed: HostLocationAddress,
    mngr_ctx: MngrContext,
    *,
    is_start_desired: bool,
) -> tuple[OnlineHostInterface, str]:
    """Resolve a HostLocationAddress to ``(host, path_str)``.

    User-supplied paths are returned verbatim, including a trailing ``/`` if the
    user typed one (``Path`` strips it, so we read the side-channel flag set by
    the parser). When the user didn't supply a path (``mngr rsync ./foo my-agent``),
    the resolved agent or host workdir is returned with a trailing ``/`` appended
    -- that's rsync's "copy contents into destination" shorthand, which is almost
    always what the user wants when they referred to an agent/host by name only.
    """
    resolved = resolve_host_location(parsed, mngr_ctx, is_start_desired=is_start_desired)
    if parsed.path is None:
        # mngr-generated path (the agent/host workdir): suffix with ``/`` so
        # rsync copies contents into destination.
        path_str = str(resolved.location.path)
        if not path_str.endswith("/"):
            path_str += "/"
        return resolved.location.host, path_str
    return resolved.location.host, _user_path_to_str(resolved.location.path, parsed.has_trailing_path_slash)


@click.command(context_settings={"ignore_unknown_options": True})
@click.argument("source", type=HOST_LOCATION_ADDRESS, metavar="SOURCE")
@click.argument("destination", type=HOST_LOCATION_ADDRESS, metavar="DESTINATION")
@click.argument("rsync_args", nargs=-1, type=click.UNPROCESSED, metavar="[-- RSYNC_ARGS...]")
@optgroup.group("Sync Options")
@optgroup.option(
    "--start/--no-start",
    default=True,
    show_default=True,
    help="Automatically start a host if offline (the agent does not need to be running)",
)
@optgroup.option(
    "--uncommitted-changes",
    type=click.Choice(["stash", "clobber", "merge", "fail"], case_sensitive=False),
    default="fail",
    show_default=True,
    help=(
        "How to handle uncommitted changes on the side being modified (the destination): "
        "stash (stash and leave stashed), clobber (overwrite), "
        "merge (stash, sync, then unstash), fail (error if changes exist)"
    ),
)
@optgroup.group("File Filtering")
@optgroup.option(
    "--include-gitignored",
    is_flag=True,
    help="Include files that match .gitignore patterns [future]",
)
@add_common_options
@click.pass_context
def rsync_command(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="rsync",
        command_class=RsyncCliOptions,
    )

    if opts.include_gitignored:
        raise NotImplementedError("--include-gitignored is not implemented yet")

    if opts.source.path is None and opts.source.agent is None:
        raise UserInputError("SOURCE must include an agent, a host, or a path")
    if opts.destination.path is None and opts.destination.agent is None:
        raise UserInputError("DESTINATION must include an agent, a host, or a path")

    # Reject when both sides are bare local paths (the user should use plain rsync
    # for that). An endpoint that names an agent or host is allowed even when it
    # resolves to a host with ``is_local=True`` -- the user clearly meant to address
    # something through mngr.
    source_is_bare_local = opts.source.agent is None and opts.source.host is None
    destination_is_bare_local = opts.destination.agent is None and opts.destination.host is None
    if source_is_bare_local and destination_is_bare_local:
        raise RsyncEndpointError(
            "mngr rsync requires one of SOURCE or DESTINATION to reference an agent or remote host"
        )

    source_host, source_path_str = _resolve_endpoint(opts.source, mngr_ctx, is_start_desired=opts.start)
    destination_host, destination_path_str = _resolve_endpoint(opts.destination, mngr_ctx, is_start_desired=opts.start)

    uncommitted_changes_mode = UncommittedChangesMode(opts.uncommitted_changes.upper())

    rsync(
        source_host=source_host,
        source_path=source_path_str,
        destination_host=destination_host,
        destination_path=destination_path_str,
        extra_args=opts.rsync_args,
        uncommitted_changes=uncommitted_changes_mode,
        cg=mngr_ctx.concurrency_group,
        run_in_terminal=True,
    )


# Register as ``mngr rsync`` (click's default name is the function name)
rsync_command.name = "rsync"


CommandHelpMetadata(
    key="rsync",
    one_line_description="Rsync files between a local path and a remote host or agent",
    synopsis="mngr rsync SOURCE DESTINATION [--start/--no-start] [--uncommitted-changes MODE] [-- RSYNC_ARGS...]",
    description="""Rsync files between two endpoints, one of which must be on the local machine.

Each endpoint is a host-location address: ``AGENT[@HOST[.PROVIDER]][:PATH]``,
``@HOST[.PROVIDER]:PATH``, or a bare local path. The local side is implicit
for a bare path; ``./``, ``../``, ``/``, and ``~/`` prefixes are honored.

**Agent paths**: a ``:PATH`` on an agent endpoint is taken relative to that
agent's workdir unless it is absolute. ``my-agent:runtime/reports`` therefore
means ``runtime/reports`` *inside the agent's worktree*, regardless of where
you run the command; pass an absolute ``:PATH`` to target an exact location.

mngr is a thin wrapper around ``rsync``. Anything you pass after ``--`` is
forwarded verbatim (use ``--dry-run``, ``--delete``, ``--exclude=PATTERN``,
``--include-from=FILE``, etc. directly).

**Trailing slashes**: rsync interprets a trailing ``/`` on the source as "copy
contents into destination" rather than "copy source itself as a child of
destination". mngr passes your paths through unchanged, so you control the
slash. The one exception: when you reference an agent or host *by name only*
(no ``:PATH``), the resolved workdir is suffixed with ``/`` automatically --
that's almost always what you want.

Exactly one of SOURCE and DESTINATION must reference a remote host or agent;
the other must be a local path. Local-to-local and remote-to-remote transfers
are rejected -- use plain ``rsync`` for local-to-local.""",
    examples=(
        ("Push contents of an agent's workdir into a local dir", "mngr rsync my-agent ./local-copy"),
        ("Push local files into an agent's workdir", "mngr rsync ./local-src/ my-agent"),
        ("Push local dir as a child of agent's workdir (no source slash)", "mngr rsync ./local-src my-agent"),
        ("Push into a subpath of an agent", "mngr rsync ./local-src/ my-agent:subdir"),
        ("Push to a specific host path", "mngr rsync ./local-src/ @host.modal:/work"),
        ("Preview what would be transferred", "mngr rsync ./local-src my-agent -- --dry-run"),
        ("Delete files in destination that aren't in source", "mngr rsync ./local-src/ my-agent -- --delete"),
    ),
    see_also=(
        ("git", "Push or pull git commits between local and a remote agent or host"),
        ("pair", "Continuously sync files between agent and local"),
    ),
).register()


add_pager_help_option(rsync_command)
