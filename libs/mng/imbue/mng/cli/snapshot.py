from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mng.api.discover import discover_all_hosts_and_agents
from imbue.mng.api.find import group_agents_by_host
from imbue.mng.api.providers import get_all_provider_instances
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.cli.agent_addr import find_agents_by_addresses
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.default_command_group import DefaultCommandGroup
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.output_helpers import AbortError
from imbue.mng.cli.output_helpers import emit_event
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.cli.output_helpers import emit_format_template_lines
from imbue.mng.cli.output_helpers import emit_info
from imbue.mng.cli.output_helpers import format_size
from imbue.mng.cli.output_helpers import on_error
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.config.data_types import CommonCliOptions
from imbue.mng.config.data_types import MngContext
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.errors import BaseMngError
from imbue.mng.errors import HostNotFoundError
from imbue.mng.errors import SnapshotsNotSupportedError
from imbue.mng.errors import UserInputError
from imbue.mng.interfaces.data_types import SnapshotInfo
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import OutputFormat
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import SnapshotId
from imbue.mng.primitives import SnapshotName

# =============================================================================
# CLI Options
# =============================================================================


class SnapshotCreateCliOptions(CommonCliOptions):
    """Options for the snapshot create subcommand."""

    identifiers: tuple[str, ...]
    agent_list: tuple[str, ...]
    hosts: tuple[str, ...]
    all_agents: bool
    name: str | None
    dry_run: bool
    on_error: str
    # Future options
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    stdin: bool
    tag: tuple[str, ...]
    description: str | None
    restart_if_larger_than: str | None
    pause_during: bool
    wait: bool


class SnapshotListCliOptions(CommonCliOptions):
    """Options for the snapshot list subcommand."""

    identifiers: tuple[str, ...]
    agent_list: tuple[str, ...]
    hosts: tuple[str, ...]
    all_agents: bool
    limit: int | None
    # Future options
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    after: str | None
    before: str | None


class SnapshotDestroyCliOptions(CommonCliOptions):
    """Options for the snapshot destroy subcommand."""

    agents: tuple[str, ...]
    agent_list: tuple[str, ...]
    snapshots: tuple[str, ...]
    all_snapshots: bool
    force: bool
    dry_run: bool
    # Future options
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    stdin: bool


# =============================================================================
# Helper Functions
# =============================================================================


def _find_host_across_providers(
    host_identifier: str,
    mng_ctx: MngContext,
) -> tuple[HostId, ProviderInstanceName] | None:
    """Find a host by ID or name across all providers.

    Returns (host_id, provider_name) if found, or None if no provider has a matching host.
    """
    for provider in get_all_provider_instances(mng_ctx):
        try:
            host = provider.get_host(HostId(host_identifier))
            return host.id, provider.name
        except (HostNotFoundError, ValueError):
            pass
        try:
            host = provider.get_host(HostName(host_identifier))
            return host.id, provider.name
        except (HostNotFoundError, ValueError):
            pass
    return None


def _classify_mixed_identifiers(
    identifiers: list[str],
    mng_ctx: MngContext,
) -> tuple[list[str], list[str]]:
    """Classify mixed identifiers into agent and host identifiers.

    Each identifier is checked against known agent names and IDs.
    If it matches an agent, it's treated as an agent identifier.
    Otherwise, it's treated as a host identifier.

    Returns (agent_identifiers, host_identifiers).
    """
    if not identifiers:
        return [], []

    # Use try/except to gracefully handle provider errors (e.g. unreachable providers).
    # Partial results are acceptable here since we're only classifying identifiers.
    try:
        agents_by_host, _ = discover_all_hosts_and_agents(mng_ctx, include_destroyed=False)
    except BaseMngError as e:
        logger.warning("Failed to load agents for identifier classification: {}", e)
        # Treat all identifiers as host identifiers when agents cannot be loaded
        return [], identifiers

    known_names_and_ids: set[str] = set()
    for agent_refs in agents_by_host.values():
        for agent_ref in agent_refs:
            known_names_and_ids.add(str(agent_ref.agent_name))
            known_names_and_ids.add(str(agent_ref.agent_id))

    agent_ids: list[str] = []
    host_ids: list[str] = []
    for identifier in identifiers:
        if identifier in known_names_and_ids:
            agent_ids.append(identifier)
        else:
            host_ids.append(identifier)

    return agent_ids, host_ids


def _resolve_snapshot_hosts(
    agent_identifiers: list[str],
    host_identifiers: list[str],
    all_agents: bool,
    mng_ctx: MngContext,
) -> list[tuple[str, ProviderInstanceName, list[str]]]:
    """Resolve agent and host identifiers to unique host targets.

    Returns a list of (host_id_str, provider_name, agent_names) tuples,
    deduplicated by host.
    """
    seen_hosts: dict[str, tuple[ProviderInstanceName, list[str]]] = {}

    # Resolve from agent identifiers
    if agent_identifiers or all_agents:
        agents = find_agents_by_addresses(
            raw_identifiers=agent_identifiers,
            filter_all=all_agents,
            target_state=AgentLifecycleState.RUNNING,
            mng_ctx=mng_ctx,
        )
        agents_by_host = group_agents_by_host(agents)
        for _host_key, agent_list in agents_by_host.items():
            host_id_str = str(agent_list[0].host_id)
            provider_name = agent_list[0].provider_name
            agent_names = [str(m.agent_name) for m in agent_list]
            if host_id_str in seen_hosts:
                existing_provider, existing_agents = seen_hosts[host_id_str]
                seen_hosts[host_id_str] = (existing_provider, existing_agents + agent_names)
            else:
                seen_hosts[host_id_str] = (provider_name, agent_names)

    # Resolve from host identifiers. These identifiers already failed agent
    # lookup in _classify_mixed_identifiers, so if host lookup also fails,
    # the error should mention both.
    for host_str in host_identifiers:
        result = _find_host_across_providers(host_str, mng_ctx)
        if result is None:
            raise UserInputError(f"Agent or host not found: {host_str}")
        host_id, provider_name = result
        host_id_str = str(host_id)
        if host_id_str not in seen_hosts:
            seen_hosts[host_id_str] = (provider_name, [])

    return [(host_id_str, prov, agents) for host_id_str, (prov, agents) in seen_hosts.items()]


def _check_create_future_options(opts: SnapshotCreateCliOptions) -> None:
    """Raise NotImplementedError for unimplemented create options."""
    if opts.include:
        raise NotImplementedError("--include is not implemented yet")
    if opts.exclude:
        raise NotImplementedError("--exclude is not implemented yet")
    if opts.stdin:
        raise NotImplementedError("--stdin is not implemented yet")
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
    if opts.include:
        raise NotImplementedError("--include is not implemented yet")
    if opts.exclude:
        raise NotImplementedError("--exclude is not implemented yet")
    if opts.after is not None:
        raise NotImplementedError("--after is not implemented yet")
    if opts.before is not None:
        raise NotImplementedError("--before is not implemented yet")


def _check_destroy_future_options(opts: SnapshotDestroyCliOptions) -> None:
    """Raise NotImplementedError for unimplemented destroy options."""
    if opts.include:
        raise NotImplementedError("--include is not implemented yet")
    if opts.exclude:
        raise NotImplementedError("--exclude is not implemented yet")
    if opts.stdin:
        raise NotImplementedError("--stdin is not implemented yet")


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
            emit_final_json(data)
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
            emit_final_json({"snapshots": data, "count": len(data)})
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
            emit_final_json({"snapshots_destroyed": destroyed, "count": len(destroyed)})
        case OutputFormat.JSONL:
            emit_event("destroy_result", {"count": len(destroyed)}, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            if destroyed:
                write_human_line("Destroyed {} snapshot(s)", len(destroyed))
        case _ as unreachable:
            assert_never(unreachable)


# =============================================================================
# CLI Group
# =============================================================================


class _SnapshotGroup(DefaultCommandGroup):
    """Snapshot command group with configurable default subcommand.

    Like the top-level mng group, bare invocation shows help by default.
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
@optgroup.group("Target Selection")
@optgroup.option(
    "--agent",
    "agent_list",
    multiple=True,
    help="Agent name or ID to snapshot (can be specified multiple times)",
)
@optgroup.option(
    "--host",
    "hosts",
    multiple=True,
    help="Host ID or name to snapshot directly (can be specified multiple times)",
)
@optgroup.option(
    "-a",
    "--all",
    "--all-agents",
    "all_agents",
    is_flag=True,
    help="Snapshot all running agents",
)
@optgroup.group("Snapshot Options")
@optgroup.option(
    "--name",
    default=None,
    help="Custom name for the snapshot",
)
@optgroup.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be snapshotted without actually creating snapshots",
)
@optgroup.option(
    "--include",
    multiple=True,
    help="Filter agents by CEL expression (repeatable) [future]",
)
@optgroup.option(
    "--exclude",
    multiple=True,
    help="Exclude agents matching CEL expression (repeatable) [future]",
)
@optgroup.option(
    "--stdin",
    is_flag=True,
    help="Read agent/host names from stdin [future]",
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
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="snapshot_create",
        command_class=SnapshotCreateCliOptions,
        is_format_template_supported=True,
    )
    logger.debug("Started snapshot create command")

    _check_create_future_options(opts)

    # Classify mixed positional identifiers as agents or hosts
    mixed_agent_ids, mixed_host_ids = _classify_mixed_identifiers(list(opts.identifiers), mng_ctx)

    # Combine with explicit --agent and --host options
    agent_identifiers = mixed_agent_ids + list(opts.agent_list)
    host_identifiers = mixed_host_ids + list(opts.hosts)

    if not agent_identifiers and not host_identifiers and not opts.all_agents:
        raise click.UsageError("Must specify at least one agent, host, or use --all")

    if (agent_identifiers or host_identifiers) and opts.all_agents:
        raise click.UsageError("Cannot specify both agent/host names and --all")

    error_behavior = ErrorBehavior(opts.on_error.upper())

    # Resolve targets to unique hosts
    targets = _resolve_snapshot_hosts(
        agent_identifiers=agent_identifiers,
        host_identifiers=host_identifiers,
        all_agents=opts.all_agents,
        mng_ctx=mng_ctx,
    )

    if not targets:
        emit_info("No hosts found to snapshot", output_opts.output_format)
        return

    # Dry run
    if opts.dry_run:
        for host_id_str, provider_name, agent_names in targets:
            agents_str = f" (agents: {', '.join(agent_names)})" if agent_names else ""
            msg = f"Would snapshot host {host_id_str} via {provider_name}{agents_str}"
            emit_event(
                "dry_run",
                {"message": msg, "host_id": host_id_str, "provider": str(provider_name)},
                output_opts.output_format,
            )
        return

    # Create snapshots
    snapshot_name = SnapshotName(opts.name) if opts.name else None
    created: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for host_id_str, provider_name, agent_names in targets:
        try:
            provider = get_provider_instance(provider_name, mng_ctx)
            if not provider.supports_snapshots:
                raise SnapshotsNotSupportedError(provider_name)

            host_id = HostId(host_id_str)
            snapshot_id = provider.create_snapshot(host_id, name=snapshot_name)

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
        except BaseMngError as e:
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
@optgroup.group("Target Selection")
@optgroup.option(
    "--agent",
    "agent_list",
    multiple=True,
    help="Agent name or ID to list snapshots for (can be specified multiple times)",
)
@optgroup.option(
    "--host",
    "hosts",
    multiple=True,
    help="Host ID or name to list snapshots for directly (can be specified multiple times)",
)
@optgroup.option(
    "-a",
    "--all",
    "--all-agents",
    "all_agents",
    is_flag=True,
    help="List snapshots for all running agents",
)
@optgroup.group("Filtering")
@optgroup.option(
    "--limit",
    type=int,
    default=None,
    help="Maximum number of snapshots to show",
)
@optgroup.option(
    "--include",
    multiple=True,
    help="Filter snapshots by CEL expression (repeatable) [future]",
)
@optgroup.option(
    "--exclude",
    multiple=True,
    help="Exclude snapshots matching CEL expression (repeatable) [future]",
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
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="snapshot_list",
        command_class=SnapshotListCliOptions,
        is_format_template_supported=True,
    )
    logger.debug("Started snapshot list command")

    _check_list_future_options(opts)

    # Classify mixed positional identifiers as agents or hosts
    mixed_agent_ids, mixed_host_ids = _classify_mixed_identifiers(list(opts.identifiers), mng_ctx)

    # Combine with explicit --agent and --host options
    agent_identifiers = mixed_agent_ids + list(opts.agent_list)
    host_identifiers = mixed_host_ids + list(opts.hosts)

    if not agent_identifiers and not host_identifiers and not opts.all_agents:
        raise click.UsageError("Must specify at least one agent, host, or use --all")

    if (agent_identifiers or host_identifiers) and opts.all_agents:
        raise click.UsageError("Cannot specify both agent/host names and --all")

    # Resolve to hosts
    targets = _resolve_snapshot_hosts(
        agent_identifiers=agent_identifiers,
        host_identifiers=host_identifiers,
        all_agents=opts.all_agents,
        mng_ctx=mng_ctx,
    )

    if not targets:
        emit_info("No hosts found", output_opts.output_format)
        return

    # Collect snapshots from all hosts
    all_snapshots: list[tuple[str, SnapshotInfo]] = []

    for host_id_str, provider_name, _agent_names in targets:
        provider = get_provider_instance(provider_name, mng_ctx)
        if not provider.supports_snapshots:
            raise SnapshotsNotSupportedError(provider_name)

        host_id = HostId(host_id_str)
        snapshots = provider.list_snapshots(host_id)
        for snap in snapshots:
            all_snapshots.append((host_id_str, snap))

    # Apply limit
    limited_snapshots = all_snapshots[: opts.limit] if opts.limit is not None else all_snapshots

    _emit_list_snapshots(limited_snapshots, output_opts)


# =============================================================================
# destroy subcommand
# =============================================================================


@snapshot.command(name="destroy")
@click.argument("agents", nargs=-1, required=False)
@optgroup.group("Target Selection")
@optgroup.option(
    "--agent",
    "agent_list",
    multiple=True,
    help="Agent name or ID whose snapshots to destroy (can be specified multiple times)",
)
@optgroup.option(
    "--snapshot",
    "snapshots",
    multiple=True,
    help="Snapshot ID to destroy (can be specified multiple times)",
)
@optgroup.option(
    "--all-snapshots",
    is_flag=True,
    help="Destroy all snapshots for the specified agent(s)",
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
    help="Show what would be destroyed without actually deleting",
)
@optgroup.option(
    "--include",
    multiple=True,
    help="Filter snapshots by CEL expression (repeatable) [future]",
)
@optgroup.option(
    "--exclude",
    multiple=True,
    help="Exclude snapshots matching CEL expression (repeatable) [future]",
)
@optgroup.option(
    "--stdin",
    is_flag=True,
    help="Read agent/host names from stdin [future]",
)
@add_common_options
@click.pass_context
def snapshot_destroy(ctx: click.Context, **kwargs: Any) -> None:
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="snapshot_destroy",
        command_class=SnapshotDestroyCliOptions,
        is_format_template_supported=True,
    )
    logger.debug("Started snapshot destroy command")

    _check_destroy_future_options(opts)

    # Validate inputs
    agent_identifiers = list(opts.agents) + list(opts.agent_list)

    if not agent_identifiers:
        raise click.UsageError("Must specify at least one agent")

    if not opts.snapshots and not opts.all_snapshots:
        raise click.UsageError("Must specify --snapshot or --all-snapshots")

    if opts.snapshots and opts.all_snapshots:
        raise click.UsageError("Cannot specify both --snapshot and --all-snapshots")

    # Resolve to hosts
    targets = _resolve_snapshot_hosts(
        agent_identifiers=agent_identifiers,
        host_identifiers=[],
        all_agents=False,
        mng_ctx=mng_ctx,
    )

    if not targets:
        emit_info("No hosts found", output_opts.output_format)
        return

    # Determine which snapshots to delete
    snapshots_to_delete: list[tuple[str, ProviderInstanceName, SnapshotId, str]] = []

    for host_id_str, provider_name, _agent_names in targets:
        provider = get_provider_instance(provider_name, mng_ctx)
        if not provider.supports_snapshots:
            raise SnapshotsNotSupportedError(provider_name)

        host_id = HostId(host_id_str)

        if opts.all_snapshots:
            existing = provider.list_snapshots(host_id)
            for snap in existing:
                snapshots_to_delete.append((host_id_str, provider_name, snap.id, str(snap.name)))
        else:
            for snap_id_str in opts.snapshots:
                snapshots_to_delete.append((host_id_str, provider_name, SnapshotId(snap_id_str), snap_id_str))

    if not snapshots_to_delete:
        emit_info("No snapshots found to destroy", output_opts.output_format)
        _emit_destroy_result([], output_opts)
        return

    # Dry run
    if opts.dry_run:
        for host_id_str, _prov, snap_id, snap_name in snapshots_to_delete:
            msg = f"Would destroy snapshot {snap_id} ({snap_name}) on host {host_id_str}"
            emit_event(
                "dry_run",
                {"message": msg, "snapshot_id": str(snap_id), "host_id": host_id_str},
                output_opts.output_format,
            )
        return

    # Confirmation prompt (human mode only, unless --force)
    if not opts.force and output_opts.output_format == OutputFormat.HUMAN:
        write_human_line("The following {} snapshot(s) will be destroyed:", len(snapshots_to_delete))
        for host_id_str, _prov, snap_id, snap_name in snapshots_to_delete:
            write_human_line("  - {} ({}) on host {}", snap_id, snap_name, host_id_str)
        if not click.confirm("Proceed?"):
            write_human_line("Aborted")
            return

    # Delete snapshots
    destroyed: list[dict[str, Any]] = []

    for host_id_str, provider_name, snap_id, _snap_name in snapshots_to_delete:
        provider = get_provider_instance(provider_name, mng_ctx)
        host_id = HostId(host_id_str)
        provider.delete_snapshot(host_id, snap_id)

        result = {
            "snapshot_id": str(snap_id),
            "host_id": host_id_str,
            "provider": str(provider_name),
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
    synopsis="mng [snapshot|snap] [create|list|destroy] [AGENTS...] [OPTIONS]",
    description="""Snapshots capture the complete filesystem state of a host, allowing it to be
restored later. Because the snapshot is at the host level, the state of all
agents on the host is saved.

Positional arguments to 'create' can be agent names/IDs or host names/IDs.
Each identifier is automatically resolved: if it matches a known agent, that
agent's host is used; otherwise it is treated as a host identifier.

When no subcommand is given, defaults to 'create'. For example,
``mng snapshot my-agent`` is equivalent to ``mng snapshot create my-agent``.

Useful for checkpointing work, creating restore points, or managing disk space.""",
    aliases=("snap",),
    examples=(
        ("Snapshot an agent's host (short form)", "mng snapshot my-agent"),
        ("Snapshot an agent's host (explicit)", "mng snapshot create my-agent"),
        ("Create a named snapshot", "mng snapshot create my-agent --name before-refactor"),
        ("Snapshot by host ID", "mng snapshot create my-host-id"),
        ("Snapshot all running agents", "mng snapshot create --all --dry-run"),
        ("List snapshots for an agent", "mng snapshot list my-agent"),
        ("Destroy all snapshots for an agent", "mng snapshot destroy my-agent --all-snapshots --force"),
        ("Preview what would be destroyed", "mng snapshot destroy my-agent --all-snapshots --dry-run"),
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
    synopsis="mng snapshot create [IDENTIFIERS...] [OPTIONS]",
    description="""Positional arguments can be agent names/IDs or host names/IDs. Each
identifier is automatically resolved: if it matches a known agent, that
agent's host is snapshotted; otherwise it is treated as a host identifier.
Multiple identifiers that resolve to the same host are deduplicated.

Supports custom format templates via --format. Available fields:
snapshot_id, host_id, provider, agent_names.""",
    examples=(
        ("Snapshot an agent's host", "mng snapshot create my-agent"),
        ("Create a named snapshot", "mng snapshot create my-agent --name before-refactor"),
        ("Snapshot all running agents (dry run)", "mng snapshot create --all --dry-run"),
        ("Snapshot multiple agents", "mng snapshot create agent1 agent2 --on-error continue"),
        ("Custom format template output", "mng snapshot create my-agent --format '{snapshot_id}'"),
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
    synopsis="mng snapshot list [IDENTIFIERS...] [OPTIONS]",
    description="""Shows snapshot ID, name, creation time, size, and host for each snapshot.

Positional arguments can be agent names/IDs or host names/IDs. Each
identifier is automatically resolved: if it matches a known agent, that
agent's host is used; otherwise it is treated as a host identifier.

Supports custom format templates via --format. Available fields:
id, name, created_at, size, size_bytes, host_id.""",
    examples=(
        ("List snapshots for an agent", "mng snapshot list my-agent"),
        ("List snapshots for all running agents", "mng snapshot list --all"),
        ("Limit number of results", "mng snapshot list my-agent --limit 5"),
        ("Output as JSON", "mng snapshot list my-agent --format json"),
        ("Custom format template", "mng snapshot list my-agent --format '{name}\\t{size}\\t{host_id}'"),
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
    synopsis="mng snapshot destroy [AGENTS...] [OPTIONS]",
    description="""Requires either --snapshot (to delete specific snapshots) or --all-snapshots
(to delete all snapshots for the resolved hosts). A confirmation prompt is
shown unless --force is specified.

Supports custom format templates via --format. Available fields:
snapshot_id, host_id, provider.""",
    examples=(
        ("Destroy a specific snapshot", "mng snapshot destroy my-agent --snapshot snap-abc123 --force"),
        ("Destroy all snapshots for an agent", "mng snapshot destroy my-agent --all-snapshots --force"),
        ("Preview what would be destroyed", "mng snapshot destroy my-agent --all-snapshots --dry-run"),
    ),
    see_also=(
        ("snapshot create", "Create a new snapshot"),
        ("snapshot list", "List existing snapshots"),
    ),
).register()
add_pager_help_option(snapshot_destroy)
