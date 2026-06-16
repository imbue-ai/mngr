from collections.abc import Sequence
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import filter_all_hosts
from imbue.mngr.api.find import filter_one_agent
from imbue.mngr.api.find import filter_one_host
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.address_params import parse_agent_or_host_addresses_or_raise
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.default_command_group import DefaultCommandGroup
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import emit_format_template_lines
from imbue.mngr.cli.output_helpers import emit_info
from imbue.mngr.cli.output_helpers import format_size
from imbue.mngr.cli.output_helpers import on_error
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.cli.stdin_utils import STDIN_PLACEHOLDER
from imbue.mngr.cli.stdin_utils import expand_stdin_placeholder
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import SnapshotsNotSupportedError
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentOrHostAddress
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName

# =============================================================================
# CLI Options
# =============================================================================


class SnapshotCreateCliOptions(CommonCliOptions):
    """Options for the snapshot create subcommand."""

    identifiers: tuple[str, ...]
    name: str | None
    on_error: str
    # Future options
    tag: tuple[str, ...]
    description: str | None
    restart_if_larger_than: str | None
    pause_during: bool
    wait: bool


class SnapshotListCliOptions(CommonCliOptions):
    """Options for the snapshot list subcommand."""

    identifiers: tuple[str, ...]
    limit: int | None
    # Future options
    after: str | None
    before: str | None


class SnapshotDestroyCliOptions(CommonCliOptions):
    """Options for the snapshot destroy subcommand."""

    identifiers: tuple[str, ...]
    snapshots: tuple[str, ...]
    all_snapshots: bool
    force: bool
    dry_run: bool


# =============================================================================
# Helper Functions
# =============================================================================


@pure
def _required_providers_for_targets(
    addresses: Sequence[AgentOrHostAddress],
) -> tuple[ProviderInstanceName, ...] | None:
    """Return the providers a discovery call can be restricted to for these targets.

    Returns the deduped tuple only when every address (agent or host) pins a
    provider, since a single un-pinned address could live on any provider.
    Empty input returns ``None`` (no addresses, no narrowing).
    """
    providers: set[ProviderInstanceName] = set()
    for addr in addresses:
        if isinstance(addr, AgentAddress):
            provider = addr.host.provider if addr.host is not None else None
        else:
            provider = addr.provider
        if provider is None:
            return None
        providers.add(provider)
    if not providers:
        return None
    return tuple(sorted(providers))


@pure
def _agent_identifiers_for_targets(
    addresses: Sequence[AgentOrHostAddress],
) -> tuple[str, ...] | None:
    """Return agent identifiers usable for the discovery event-stream optimization.

    The discovery event stream only knows about agents, so we can only feed it
    identifiers when *every* address is an :class:`AgentAddress` -- otherwise
    a host-only address could live on a provider not covered by any agent
    identifier and would be silently skipped.
    """
    identifiers: list[str] = []
    for addr in addresses:
        if not isinstance(addr, AgentAddress):
            return None
        identifiers.append(str(addr.agent))
    return tuple(identifiers) if identifiers else None


def _find_snapshot_targets(
    addresses: Sequence[AgentOrHostAddress],
    mngr_ctx: MngrContext,
) -> dict[DiscoveredHost, list[AgentName]]:
    """Resolve a mixed sequence of agent and host addresses to their target hosts.

    Each :class:`AgentAddress` contributes one ``(host, agent_name)`` pair: the
    agent is resolved uniquely (raising on ambiguity via
    :func:`filter_one_agent`) and its name appended under its host. Each
    :class:`HostAddress` contributes every matching host with an empty name
    list; if a host is also referenced by an agent address, the agent name
    stays in that host's list. The agent names are kept purely for display
    in the per-host snapshot result -- snapshots operate at the host level.

    Discovery is narrowed to the addresses' providers when every address pins
    one, and to the agent identifiers (via the discovery event stream) when
    every address is an :class:`AgentAddress`.
    """
    provider_names = _required_providers_for_targets(addresses)
    agent_identifiers: tuple[str, ...] | None = None
    if provider_names is None:
        agent_identifiers = _agent_identifiers_for_targets(addresses)

    agents_by_host, _ = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=tuple(str(p) for p in provider_names) if provider_names is not None else None,
        agent_identifiers=agent_identifiers,
        include_destroyed=False,
        reset_caches=False,
    )

    all_hosts = list(agents_by_host.keys())
    result: dict[DiscoveredHost, list[AgentName]] = {}

    for addr in addresses:
        if isinstance(addr, AgentAddress):
            host_constraint: DiscoveredHost | None = None
            if addr.host is not None:
                host_constraint = filter_one_host(addr.host, all_hosts)
            host_ref, agent_ref = filter_one_agent(addr.agent, host_constraint, agents_by_host)
            result.setdefault(host_ref, []).append(agent_ref.agent_name)
        else:
            matches = filter_all_hosts(addr, all_hosts)
            if not matches:
                raise UserInputError(f"Agent or host not found: {addr}")
            for host_ref in matches:
                result.setdefault(host_ref, [])

    return result


def _check_create_future_options(opts: SnapshotCreateCliOptions) -> None:
    """Raise NotImplementedError for unimplemented create options."""
    if opts.tag:
        raise NotImplementedError("--tag is not implemented yet")
    if opts.description is not None:
        raise NotImplementedError("--description is not implemented yet")
    if opts.restart_if_larger_than is not None:
        raise NotImplementedError("--restart-if-larger-than is not implemented yet")
    if not opts.pause_during:
        raise NotImplementedError("--no-pause-during is not implemented yet")
    if not opts.wait:
        raise NotImplementedError("--no-wait is not implemented yet")


def _check_list_future_options(opts: SnapshotListCliOptions) -> None:
    """Raise NotImplementedError for unimplemented list options."""
    if opts.after is not None:
        raise NotImplementedError("--after is not implemented yet")
    if opts.before is not None:
        raise NotImplementedError("--before is not implemented yet")


# =============================================================================
# Output Helpers
# =============================================================================


def _emit_create_result(
    created: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    output_opts: OutputOptions,
) -> None:
    """Emit final output for snapshot create."""
    if output_opts.format_template is not None:
        items: list[dict[str, str]] = []
        for entry in created:
            items.append(
                {
                    "snapshot_id": entry["snapshot_id"],
                    "host_id": entry["host_id"],
                    "provider": entry["provider"],
                    "agent_names": ", ".join(entry["agent_names"]),
                }
            )
        emit_format_template_lines(output_opts.format_template, items)
        return
    match output_opts.output_format:
        case OutputFormat.JSON:
            data: dict[str, Any] = {"snapshots_created": created, "count": len(created)}
            if errors:
                data["errors"] = errors
                data["error_count"] = len(errors)
            write_json_line(data)
        case OutputFormat.JSONL:
            event_data: dict[str, Any] = {"count": len(created)}
            if errors:
                event_data["error_count"] = len(errors)
            emit_event("create_result", event_data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            if created:
                write_human_line("Created {} snapshot(s)", len(created))
            if errors:
                logger.warning("Failed to create {} snapshot(s)", len(errors))
        case _ as unreachable:
            assert_never(unreachable)


def _emit_list_snapshots(
    # List of (host_id_str, SnapshotInfo) tuples
    all_snapshots: list[tuple[str, SnapshotInfo]],
    output_opts: OutputOptions,
) -> None:
    """Emit output for snapshot list."""
    if output_opts.format_template is not None:
        items: list[dict[str, str]] = []
        for host_id, snap in all_snapshots:
            items.append(
                {
                    "id": str(snap.id),
                    "name": str(snap.name),
                    "created_at": snap.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "size": format_size(snap.size_bytes) if snap.size_bytes is not None else "-",
                    "size_bytes": str(snap.size_bytes) if snap.size_bytes is not None else "",
                    "host_id": host_id,
                }
            )
        emit_format_template_lines(output_opts.format_template, items)
        return
    match output_opts.output_format:
        case OutputFormat.JSON:
            data = [
                {
                    "host_id": host_id,
                    **snap.model_dump(mode="json"),
                }
                for host_id, snap in all_snapshots
            ]
            write_json_line({"snapshots": data, "count": len(data)})
        case OutputFormat.JSONL:
            for host_id, snap in all_snapshots:
                emit_event(
                    "snapshot",
                    {"host_id": host_id, **snap.model_dump(mode="json")},
                    OutputFormat.JSONL,
                )
        case OutputFormat.HUMAN:
            if not all_snapshots:
                write_human_line("No snapshots found")
                return
            # Table header
            write_human_line("{:<40} {:<25} {:<22} {:<12} {}", "ID", "NAME", "CREATED", "SIZE", "HOST")
            write_human_line("{}", "-" * 110)
            for host_id, snap in all_snapshots:
                size_str = format_size(snap.size_bytes) if snap.size_bytes is not None else "-"
                created_str = snap.created_at.strftime("%Y-%m-%d %H:%M:%S")
                write_human_line(
                    "{:<40} {:<25} {:<22} {:<12} {}",
                    str(snap.id),
                    str(snap.name),
                    created_str,
                    size_str,
                    host_id,
                )
        case _ as unreachable:
            assert_never(unreachable)


def _emit_destroy_result(
    destroyed: list[dict[str, Any]],
    output_opts: OutputOptions,
) -> None:
    """Emit final output for snapshot destroy."""
    if output_opts.format_template is not None:
        items: list[dict[str, str]] = []
        for entry in destroyed:
            items.append(
                {
                    "snapshot_id": entry["snapshot_id"],
                    "host_id": entry["host_id"],
                    "provider": entry["provider"],
                }
            )
        emit_format_template_lines(output_opts.format_template, items)
        return
    match output_opts.output_format:
        case OutputFormat.JSON:
            write_json_line({"snapshots_destroyed": destroyed, "count": len(destroyed)})
        case OutputFormat.JSONL:
            emit_event("destroy_result", {"count": len(destroyed)}, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            if destroyed:
                write_human_line("Destroyed {} snapshot(s)", len(destroyed))
        case _ as unreachable:
            assert_never(unreachable)


def _emit_destroy_dry_run(
    # List of (host_ref, SnapshotId, snapshot_name) tuples
    snapshots_to_delete: list[tuple[DiscoveredHost, SnapshotId, str]],
    output_opts: OutputOptions,
) -> None:
    """Emit output describing what a snapshot destroy would do, without deleting."""
    entries = [
        {
            "snapshot_id": str(snap_id),
            "snapshot_name": snap_name,
            "host_id": str(host_ref.host_id),
            "provider": str(host_ref.provider_name),
        }
        for host_ref, snap_id, snap_name in snapshots_to_delete
    ]
    if output_opts.format_template is not None:
        emit_format_template_lines(output_opts.format_template, entries)
        return
    match output_opts.output_format:
        case OutputFormat.JSON:
            write_json_line({"dry_run": True, "snapshots": entries, "count": len(entries)})
        case OutputFormat.JSONL:
            emit_event("dry_run", {"snapshots": entries, "count": len(entries)}, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            write_human_line("Would destroy {} snapshot(s):", len(entries))
            for host_ref, snap_id, snap_name in snapshots_to_delete:
                write_human_line("  - {} ({}) on host {}", snap_id, snap_name, host_ref.host_id)
        case _ as unreachable:
            assert_never(unreachable)


# =============================================================================
# CLI Group
# =============================================================================


class _SnapshotGroup(DefaultCommandGroup):
    """Snapshot command group with configurable default subcommand.

    Like the top-level mngr group, bare invocation shows help by default.
    Users can set ``[commands.snapshot] default_subcommand = "create"``
    in config to restore the old forwarding behavior.
    """

    _config_key = "snapshot"


@click.group(name="snapshot", cls=_SnapshotGroup)
@add_common_options
@click.pass_context
def snapshot(ctx: click.Context, **kwargs: Any) -> None:
    pass


# =============================================================================
# create subcommand
# =============================================================================


@snapshot.command(name="create")
@click.argument("identifiers", nargs=-1, required=False)
@optgroup.group("Snapshot Options")
@optgroup.option(
    "--name",
    default=None,
    help="Custom name for the snapshot",
)
@optgroup.option(
    "--tag",
    multiple=True,
    help="Metadata tag for the snapshot (KEY=VALUE) [future]",
)
@optgroup.option(
    "--description",
    default=None,
    help="Description for the snapshot [future]",
)
@optgroup.option(
    "--restart-if-larger-than",
    default=None,
    help="Restart host if snapshot exceeds size (e.g., 5G) [future]",
)
@optgroup.option(
    "--pause-during/--no-pause-during",
    "pause_during",
    default=True,
    help="Pause agent during snapshot creation [future]",
)
@optgroup.option(
    "--wait/--no-wait",
    "wait",
    default=True,
    help="Wait for snapshot to complete [future]",
)
@optgroup.group("Error Handling")
@optgroup.option(
    "--on-error",
    type=click.Choice(["abort", "continue"], case_sensitive=False),
    default="continue",
    help="What to do when errors occur: abort (stop immediately) or continue (keep going)",
)
@add_common_options
@click.pass_context
def snapshot_create(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _snapshot_create_impl(ctx, **kwargs)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _snapshot_create_impl(ctx: click.Context, **kwargs: Any) -> None:
    """Implementation of snapshot create command (extracted for AbortError handling)."""
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="snapshot_create",
        command_class=SnapshotCreateCliOptions,
        is_format_template_supported=True,
    )
    logger.debug("Started snapshot create command")

    _check_create_future_options(opts)

    addresses = parse_agent_or_host_addresses_or_raise(expand_stdin_placeholder(opts.identifiers))

    if not addresses:
        if STDIN_PLACEHOLDER not in opts.identifiers:
            raise click.UsageError("Must specify at least one agent or host (use '-' to read from stdin)")
        return

    error_behavior = ErrorBehavior(opts.on_error.upper())

    targets = _find_snapshot_targets(addresses, mngr_ctx)

    if not targets:
        emit_info("No hosts found to snapshot", output_opts.output_format)
        return

    # Create snapshots
    snapshot_name = SnapshotName(opts.name) if opts.name else None
    created: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for host_ref, agent_names in targets.items():
        host_id_str = str(host_ref.host_id)
        provider_name = host_ref.provider_name
        try:
            provider = get_provider_instance(provider_name, mngr_ctx)
            if not provider.supports_snapshots:
                raise SnapshotsNotSupportedError(provider_name)

            snapshot_id = provider.create_snapshot(host_ref.host_id, name=snapshot_name)

            result = {
                "snapshot_id": str(snapshot_id),
                "host_id": host_id_str,
                "provider": str(provider_name),
                "agent_names": agent_names,
            }
            created.append(result)

            if output_opts.format_template is None:
                agents_str = f" (agents: {', '.join(agent_names)})" if agent_names else ""
                emit_event(
                    "snapshot_created",
                    {"message": f"Created snapshot {snapshot_id} for host {host_id_str}{agents_str}", **result},
                    output_opts.output_format,
                )
        except MngrError as e:
            error_msg = f"Failed to create snapshot for host {host_id_str}: {e}"
            errors.append({"host_id": host_id_str, "error": str(e)})
            on_error(error_msg, error_behavior, output_opts.output_format, exc=e)

    _emit_create_result(created, errors, output_opts)

    if errors:
        ctx.exit(1)


# =============================================================================
# list subcommand
# =============================================================================


@snapshot.command(name="list")
@click.argument("identifiers", nargs=-1, required=False)
@optgroup.group("Filtering")
@optgroup.option(
    "--limit",
    type=int,
    default=None,
    help="Maximum number of snapshots to show",
)
@optgroup.option(
    "--after",
    default=None,
    help="Show only snapshots created after this date [future]",
)
@optgroup.option(
    "--before",
    default=None,
    help="Show only snapshots created before this date [future]",
)
@add_common_options
@click.pass_context
def snapshot_list(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="snapshot_list",
        command_class=SnapshotListCliOptions,
        is_format_template_supported=True,
    )
    logger.debug("Started snapshot list command")

    _check_list_future_options(opts)

    addresses = parse_agent_or_host_addresses_or_raise(expand_stdin_placeholder(opts.identifiers))

    if not addresses:
        if STDIN_PLACEHOLDER not in opts.identifiers:
            raise click.UsageError("Must specify at least one agent or host (use '-' to read from stdin)")
        return

    targets = _find_snapshot_targets(addresses, mngr_ctx)

    if not targets:
        emit_info("No hosts found", output_opts.output_format)
        return

    # Collect snapshots from all hosts
    all_snapshots: list[tuple[str, SnapshotInfo]] = []

    for host_ref in targets:
        provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
        if not provider.supports_snapshots:
            raise SnapshotsNotSupportedError(host_ref.provider_name)

        host_id_str = str(host_ref.host_id)
        snapshots = provider.list_snapshots(host_ref.host_id)
        for snap in snapshots:
            all_snapshots.append((host_id_str, snap))

    # Apply limit
    limited_snapshots = all_snapshots[: opts.limit] if opts.limit is not None else all_snapshots

    _emit_list_snapshots(limited_snapshots, output_opts)


# =============================================================================
# destroy subcommand
# =============================================================================


@snapshot.command(name="destroy")
@click.argument("identifiers", nargs=-1, required=False)
@optgroup.group("Target Selection")
@optgroup.option(
    "--snapshot",
    "snapshots",
    multiple=True,
    help="Snapshot ID to destroy (can be specified multiple times)",
)
@optgroup.option(
    "--all-snapshots",
    is_flag=True,
    help="Destroy all snapshots for the resolved hosts",
)
@optgroup.group("Safety")
@optgroup.option(
    "-f",
    "--force",
    is_flag=True,
    help="Skip confirmation prompt",
)
@optgroup.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be destroyed without actually destroying anything",
)
@add_common_options
@click.pass_context
def snapshot_destroy(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="snapshot_destroy",
        command_class=SnapshotDestroyCliOptions,
        is_format_template_supported=True,
    )
    logger.debug("Started snapshot destroy command")

    addresses = parse_agent_or_host_addresses_or_raise(expand_stdin_placeholder(opts.identifiers))

    if not addresses:
        if STDIN_PLACEHOLDER not in opts.identifiers:
            raise click.UsageError("Must specify at least one agent or host (use '-' to read from stdin)")
        return

    if not opts.snapshots and not opts.all_snapshots:
        raise click.UsageError("Must specify --snapshot or --all-snapshots")

    if opts.snapshots and opts.all_snapshots:
        raise click.UsageError("Cannot specify both --snapshot and --all-snapshots")

    targets = _find_snapshot_targets(addresses, mngr_ctx)

    if not targets:
        emit_info("No hosts found", output_opts.output_format)
        return

    # Determine which snapshots to delete
    snapshots_to_delete: list[tuple[DiscoveredHost, SnapshotId, str]] = []

    for host_ref in targets:
        provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
        if not provider.supports_snapshots:
            raise SnapshotsNotSupportedError(host_ref.provider_name)

        if opts.all_snapshots:
            existing = provider.list_snapshots(host_ref.host_id)
            for snap in existing:
                snapshots_to_delete.append((host_ref, snap.id, str(snap.name)))
        else:
            for snap_id_str in opts.snapshots:
                snapshots_to_delete.append((host_ref, SnapshotId(snap_id_str), snap_id_str))

    if not snapshots_to_delete:
        emit_info("No snapshots found to destroy", output_opts.output_format)
        _emit_destroy_result([], output_opts)
        return

    # Dry run: report what would be destroyed without deleting anything.
    if opts.dry_run:
        _emit_destroy_dry_run(snapshots_to_delete, output_opts)
        return

    # Confirmation prompt (human mode only, unless --force)
    if not opts.force and output_opts.output_format == OutputFormat.HUMAN:
        write_human_line("The following {} snapshot(s) will be destroyed:", len(snapshots_to_delete))
        for host_ref, snap_id, snap_name in snapshots_to_delete:
            write_human_line("  - {} ({}) on host {}", snap_id, snap_name, host_ref.host_id)
        if not click.confirm("Proceed?"):
            write_human_line("Aborted")
            return

    # Delete snapshots
    destroyed: list[dict[str, Any]] = []

    for host_ref, snap_id, _snap_name in snapshots_to_delete:
        provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
        provider.delete_snapshot(host_ref.host_id, snap_id)

        host_id_str = str(host_ref.host_id)
        result = {
            "snapshot_id": str(snap_id),
            "host_id": host_id_str,
            "provider": str(host_ref.provider_name),
        }
        destroyed.append(result)

        if output_opts.format_template is None:
            emit_event(
                "snapshot_destroyed",
                {"message": f"Destroyed snapshot {snap_id} on host {host_id_str}", **result},
                output_opts.output_format,
            )

    _emit_destroy_result(destroyed, output_opts)


# =============================================================================
# Help Metadata
# =============================================================================


CommandHelpMetadata(
    key="snapshot",
    one_line_description="Create, list, and destroy host snapshots",
    synopsis="mngr [snapshot|snap] [create|list|destroy] [AGENTS...|-] [OPTIONS]",
    description="""Snapshots capture the complete filesystem state of a host, allowing it to be
restored later. Because the snapshot is at the host level, the state of all
agents on the host is saved.

Positional arguments to 'create' can be agent names/IDs or host names/IDs.
Each identifier is automatically resolved: if it matches a known agent, that
agent's host is used; otherwise it is treated as a host identifier.

When no subcommand is given, defaults to 'create'. For example,
``mngr snapshot my-agent`` is equivalent to ``mngr snapshot create my-agent``.

Useful for checkpointing work, creating restore points, or managing disk space.

Use '-' in place of agent/host names to read them from stdin, one per line.""",
    aliases=("snap",),
    examples=(
        ("Snapshot an agent's host (short form)", "mngr snapshot my-agent"),
        ("Snapshot an agent's host (explicit)", "mngr snapshot create my-agent"),
        ("Create a named snapshot", "mngr snapshot create my-agent --name before-refactor"),
        ("Snapshot by host ID", "mngr snapshot create my-host-id"),
        ("Snapshot all running agents", "mngr list --ids | mngr snapshot create -"),
        ("List snapshots for an agent", "mngr snapshot list my-agent"),
        ("Destroy all snapshots for an agent", "mngr snapshot destroy my-agent --all-snapshots --force"),
        ("Destroy all snapshots for multiple agents", "mngr snapshot destroy agent1 agent2 --all-snapshots --force"),
    ),
    see_also=(
        ("create", "Create a new agent (supports --snapshot to restore from snapshot)"),
        ("gc", "Garbage collect unused resources including snapshots"),
    ),
).register()

add_pager_help_option(snapshot)

# -- Subcommand help metadata --

CommandHelpMetadata(
    key="snapshot.create",
    one_line_description="Create a snapshot of agent host(s)",
    synopsis="mngr snapshot create [IDENTIFIERS...|-] [--name <NAME>] [--on-error <MODE>]",
    description="""Positional arguments can be agent names/IDs or host names/IDs. Each
identifier is automatically resolved: if it matches a known agent, that
agent's host is snapshotted; otherwise it is treated as a host identifier.
Multiple identifiers that resolve to the same host are deduplicated.

Use '-' in place of identifiers to read them from stdin, one per line.

Supports custom format templates via --format. Available fields:
snapshot_id, host_id, provider, agent_names.""",
    examples=(
        ("Snapshot an agent's host", "mngr snapshot create my-agent"),
        ("Create a named snapshot", "mngr snapshot create my-agent --name before-refactor"),
        ("Snapshot all running agents", "mngr list --ids | mngr snapshot create -"),
        ("Snapshot multiple agents", "mngr snapshot create agent1 agent2 --on-error continue"),
        ("Custom format template output", "mngr snapshot create my-agent --format '{snapshot_id}'"),
    ),
    see_also=(
        ("snapshot list", "List existing snapshots"),
        ("snapshot destroy", "Destroy existing snapshots"),
    ),
).register()
add_pager_help_option(snapshot_create)

CommandHelpMetadata(
    key="snapshot.list",
    one_line_description="List snapshots for agent host(s)",
    synopsis="mngr snapshot list [IDENTIFIERS...|-] [--limit <N>]",
    description="""Shows snapshot ID, name, creation time, size, and host for each snapshot.

Positional arguments can be agent names/IDs or host names/IDs. Each
identifier is automatically resolved: if it matches a known agent, that
agent's host is used; otherwise it is treated as a host identifier.

Use '-' in place of identifiers to read them from stdin, one per line.

Supports custom format templates via --format. Available fields:
id, name, created_at, size, size_bytes, host_id.""",
    examples=(
        ("List snapshots for an agent", "mngr snapshot list my-agent"),
        ("List snapshots for all running agents", "mngr list --ids | mngr snapshot list -"),
        ("Limit number of results", "mngr snapshot list my-agent --limit 5"),
        ("Output as JSON", "mngr snapshot list my-agent --format json"),
        ("Custom format template", "mngr snapshot list my-agent --format '{name}\\t{size}\\t{host_id}'"),
    ),
    see_also=(
        ("snapshot create", "Create a new snapshot"),
        ("snapshot destroy", "Destroy existing snapshots"),
    ),
).register()
add_pager_help_option(snapshot_list)

CommandHelpMetadata(
    key="snapshot.destroy",
    one_line_description="Destroy snapshots for agent host(s)",
    synopsis="mngr snapshot destroy [IDENTIFIERS...|-] [--snapshot <ID>] [--all-snapshots] [-f|--force] [--dry-run]",
    description="""Requires either --snapshot (to delete specific snapshots) or --all-snapshots
(to delete all snapshots for the resolved hosts). A confirmation prompt is
shown unless --force is specified. Pass --dry-run to preview what would be
destroyed without deleting anything.

Positional arguments can be agent names/IDs or host names/IDs. Each
identifier is automatically resolved: if it matches a known agent, that
agent's host is used; otherwise it is treated as a host identifier.

Use '-' in place of identifiers to read them from stdin, one per line.

Supports custom format templates via --format. Available fields:
snapshot_id, host_id, provider.""",
    examples=(
        ("Destroy a specific snapshot", "mngr snapshot destroy my-agent --snapshot snap-abc123 --force"),
        ("Destroy all snapshots for an agent", "mngr snapshot destroy my-agent --all-snapshots --force"),
        ("Destroy all snapshots for multiple agents", "mngr snapshot destroy agent1 agent2 --all-snapshots --force"),
        ("Preview what a destroy would remove", "mngr snapshot destroy my-agent --all-snapshots --dry-run"),
    ),
    see_also=(
        ("snapshot create", "Create a new snapshot"),
        ("snapshot list", "List existing snapshots"),
    ),
).register()
add_pager_help_option(snapshot_destroy)
