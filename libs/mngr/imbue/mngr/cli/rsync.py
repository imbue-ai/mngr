import click
from click_option_group import optgroup

from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import ResolvedHostLocationAddress
from imbue.mngr.api.find import ensure_host_started
from imbue.mngr.api.find import resolve_host_location_address
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.api.sync import RsyncEndpointError
from imbue.mngr.api.sync import rsync
from imbue.mngr.cli.address_params import HOST_LOCATION_ADDRESS
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import output_rsync_result
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.primitives import HostLocationAddress
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import UncommittedChangesMode
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME


class RsyncCliOptions(CommonCliOptions):
    """Options for the rsync command."""

    source: HostLocationAddress
    destination: HostLocationAddress
    dry_run: bool
    start: bool
    delete: bool
    uncommitted_changes: str
    # Future flags (preserved as not-yet-implemented placeholders)
    exclude: tuple[str, ...]
    include: tuple[str, ...]
    include_gitignored: bool
    include_file: str | None
    exclude_file: str | None
    rsync_arg: tuple[str, ...]
    rsync_args: str | None


def _resolve_endpoint(
    parsed: HostLocationAddress,
    mngr_ctx: MngrContext,
    *,
    is_start_desired: bool,
) -> ResolvedHostLocationAddress:
    """Resolve a HostLocationAddress to a concrete (host, path), short-circuiting bare-path inputs."""
    if parsed.agent is None and parsed.host is None:
        if parsed.path is None:
            raise UserInputError("Endpoint must include an agent, a host, or a path")
        provider = get_provider_instance(LOCAL_PROVIDER_NAME, mngr_ctx)
        host = provider.get_host(HostName(LOCAL_HOST_NAME))
        online_host, _ = ensure_host_started(host, is_start_desired=is_start_desired, provider=provider)
        return ResolvedHostLocationAddress(location=HostLocation(host=online_host, path=parsed.path))

    agents_by_host, _ = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=None,
        agent_identifiers=None,
        include_destroyed=False,
        reset_caches=False,
    )
    return resolve_host_location_address(
        parsed,
        agents_by_host,
        mngr_ctx,
        is_start_desired=is_start_desired,
    )


@click.command()
@click.argument("source", type=HOST_LOCATION_ADDRESS, metavar="SOURCE")
@click.argument("destination", type=HOST_LOCATION_ADDRESS, metavar="DESTINATION")
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
    help="Automatically start a host if offline (the agent does not need to be running)",
)
@optgroup.option(
    "--delete/--no-delete",
    default=False,
    help="Delete files in destination that don't exist in source",
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
    "--exclude",
    multiple=True,
    help="Patterns to exclude from sync [repeatable] [future]",
)
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
@add_common_options
@click.pass_context
def rsync_command(ctx: click.Context, **kwargs) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="rsync",
        command_class=RsyncCliOptions,
    )

    # Reject future-only options up front
    if opts.exclude:
        raise NotImplementedError("--exclude is not implemented yet")
    if opts.include:
        raise NotImplementedError("--include is not implemented yet")
    if opts.include_gitignored:
        raise NotImplementedError("--include-gitignored is not implemented yet")
    if opts.include_file is not None:
        raise NotImplementedError("--include-file is not implemented yet")
    if opts.exclude_file is not None:
        raise NotImplementedError("--exclude-file is not implemented yet")
    if opts.rsync_arg:
        raise NotImplementedError("--rsync-arg is not implemented yet")
    if opts.rsync_args is not None:
        raise NotImplementedError("--rsync-args is not implemented yet")

    if opts.source.path is None and opts.source.agent is None:
        raise UserInputError("SOURCE must include an agent, a host, or a path")
    if opts.destination.path is None and opts.destination.agent is None:
        raise UserInputError("DESTINATION must include an agent, a host, or a path")

    source_resolved = _resolve_endpoint(opts.source, mngr_ctx, is_start_desired=opts.start)
    destination_resolved = _resolve_endpoint(opts.destination, mngr_ctx, is_start_desired=opts.start)

    uncommitted_changes_mode = UncommittedChangesMode(opts.uncommitted_changes.upper())

    # The api also enforces this; check at the CLI layer so users see a clean error
    # before discovery side effects continue.
    if source_resolved.location.host.is_local == destination_resolved.location.host.is_local:
        if source_resolved.location.host.is_local:
            raise RsyncEndpointError(
                "mngr rsync requires one of SOURCE or DESTINATION to reference a remote host or agent"
            )
        raise RsyncEndpointError("mngr rsync does not support remote-to-remote transfers")

    result = rsync(
        source_host=source_resolved.location.host,
        source_path=source_resolved.location.path,
        destination_host=destination_resolved.location.host,
        destination_path=destination_resolved.location.path,
        is_dry_run=opts.dry_run,
        is_delete=opts.delete,
        uncommitted_changes=uncommitted_changes_mode,
        cg=mngr_ctx.concurrency_group,
    )

    output_rsync_result(result, output_opts.output_format)


# Register as ``mngr rsync`` (click's default name is the function name)
rsync_command.name = "rsync"


CommandHelpMetadata(
    key="rsync",
    one_line_description="Rsync files between a local path and a remote host or agent",
    synopsis="mngr rsync SOURCE DESTINATION [--dry-run] [--delete] [--start/--no-start] [--uncommitted-changes MODE]",
    description="""Rsync files between two endpoints, one of which must be on the local machine.

Each endpoint is a host-location address: ``AGENT[@HOST[.PROVIDER]][:PATH]``,
``@HOST[.PROVIDER]:PATH``, or a bare local path. The local side is implicit
for a bare path; ``./``, ``../``, ``/``, and ``~/`` prefixes are honored.

Exactly one of SOURCE and DESTINATION must reference a remote host or agent;
the other must be a local path. Local-to-local and remote-to-remote transfers
are not supported -- use plain ``rsync`` for the former.""",
    examples=(
        ("Push local files into an agent", "mngr rsync ./local-src my-agent"),
        ("Push into a subpath of an agent", "mngr rsync ./local-src my-agent:subdir"),
        ("Pull from an agent into a local directory", "mngr rsync my-agent ./local-copy"),
        ("Pull a subpath from an agent", "mngr rsync my-agent:src ./local-src"),
        ("Push to a specific host path", "mngr rsync ./local-src @host.modal:/work"),
        ("Preview what would be transferred", "mngr rsync ./local-src my-agent --dry-run"),
    ),
    see_also=(
        ("git push", "Push git commits from local to a remote repository"),
        ("git pull", "Pull git commits from a remote repository to local"),
        ("pair", "Continuously sync files between agent and local"),
    ),
).register()


add_pager_help_option(rsync_command)
