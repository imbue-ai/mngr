from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mng.api.data_types import GcResourceTypes
from imbue.mng.api.data_types import GcResult
from imbue.mng.api.gc import gc as api_gc
from imbue.mng.api.providers import get_all_provider_instances
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.output_helpers import AbortError
from imbue.mng.cli.output_helpers import emit_event
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.cli.output_helpers import emit_info
from imbue.mng.cli.output_helpers import format_size
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.cli.watch_mode import run_watch_loop
from imbue.mng.config.data_types import CommonCliOptions
from imbue.mng.config.data_types import MngContext
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import OutputFormat
from imbue.mng.primitives import ProviderInstanceName


class GcCliOptions(CommonCliOptions):
    """Options passed from the CLI to the gc command.

    This captures all the click parameters so we can pass them as a single object
    to helper functions instead of passing dozens of individual parameters.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.

    Note that this class VERY INTENTIONALLY DOES NOT use Field() decorators with descriptions, defaults, etc.
    For that information, see the click.option() and click.argument() decorators on the gc() function itself.
    """

    dry_run: bool
    on_error: str
    all_providers: bool
    provider: tuple[str, ...]
    watch: int | None


@click.command(name="gc")
@optgroup.group("Scope")
@optgroup.option(
    "--all-providers",
    is_flag=True,
    help="Clean resources across all providers",
)
@optgroup.option(
    "--provider",
    multiple=True,
    help="Clean resources for a specific provider (repeatable)",
)
@optgroup.group("Safety")
@optgroup.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be cleaned without actually cleaning",
)
@optgroup.option(
    "--on-error",
    type=click.Choice(["abort", "continue"], case_sensitive=False),
    default="abort",
    help="What to do when errors occur: abort (stop immediately) or continue (keep going)",
)
@optgroup.option(
    "-w",
    "--watch",
    type=int,
    help="Re-run garbage collection at the specified interval (seconds)",
)
@add_common_options
@click.pass_context
def gc(ctx: click.Context, **kwargs) -> None:
    try:
        _gc_impl(ctx, **kwargs)
    except AbortError as e:
        # AbortError means we should exit immediately with an error
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _gc_impl(ctx: click.Context, **kwargs) -> None:
    """Implementation of gc command (extracted for exception handling)."""
    # Setup command context (config, logging, output options)
    # This loads the config, applies defaults, and creates the final options
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="gc",
        command_class=GcCliOptions,
    )

    # Watch mode: run gc repeatedly at the specified interval
    if opts.watch:
        try:
            run_watch_loop(
                iteration_fn=lambda: _run_gc_iteration(mng_ctx=mng_ctx, opts=opts, output_opts=output_opts),
                interval_seconds=opts.watch,
                on_error_continue=True,
            )
        except KeyboardInterrupt:
            logger.info("\nWatch mode stopped")
            return
    else:
        _run_gc_iteration(mng_ctx=mng_ctx, opts=opts, output_opts=output_opts)


def _run_gc_iteration(mng_ctx: MngContext, opts: GcCliOptions, output_opts: OutputOptions) -> None:
    """Run a single gc iteration."""
    error_behavior = ErrorBehavior(opts.on_error.upper())

    providers = _get_selected_providers(mng_ctx=mng_ctx, opts=opts)

    # Always GC all resource types
    resource_types = GcResourceTypes(
        is_machines=True,
        is_snapshots=True,
        is_volumes=True,
        is_work_dirs=True,
        is_logs=True,
        is_build_cache=True,
    )

    # Call the API
    result = api_gc(
        mng_ctx=mng_ctx,
        providers=providers,
        resource_types=resource_types,
        dry_run=opts.dry_run,
        error_behavior=error_behavior,
        on_resource_type_start=lambda rt: _emit_resource_type_start(rt, output_opts.output_format),
    )

    # Emit destroyed events for CLI output
    for work_dir in result.work_dirs_destroyed:
        _emit_destroyed("work_dir", work_dir, output_opts.output_format, opts.dry_run)
    for machine in result.machines_destroyed:
        _emit_destroyed("machine", machine, output_opts.output_format, opts.dry_run)
    for machine in result.machines_deleted:
        _emit_destroyed("machine_record", machine, output_opts.output_format, opts.dry_run)
    for snapshot in result.snapshots_destroyed:
        _emit_destroyed("snapshot", snapshot, output_opts.output_format, opts.dry_run)
    for volume in result.volumes_destroyed:
        _emit_destroyed("volume", volume, output_opts.output_format, opts.dry_run)
    for log in result.logs_destroyed:
        _emit_destroyed("log", log, output_opts.output_format, opts.dry_run)
    for cache in result.build_cache_destroyed:
        _emit_destroyed("build_cache", cache, output_opts.output_format, opts.dry_run)

    # Emit final summary
    _emit_final_summary(result=result, output_format=output_opts.output_format, dry_run=opts.dry_run)


_RESOURCE_TYPE_MESSAGES: dict[str, str] = {
    "work_dirs": "Cleaning work directories...",
    "machines": "Cleaning machines...",
    "snapshots": "Cleaning snapshots...",
    "volumes": "Cleaning volumes...",
    "logs": "Cleaning logs...",
    "build_cache": "Cleaning build cache...",
}


def _emit_resource_type_start(resource_type: str, output_format: OutputFormat) -> None:
    """Emit an info message when starting to GC a specific resource type."""
    msg = _RESOURCE_TYPE_MESSAGES.get(resource_type, f"Cleaning {resource_type}...")
    emit_info(msg, output_format)


@pure
def _format_destroyed_message(resource_type: str, resource: Any, dry_run: bool) -> str:
    """Format a human-readable message for a destroyed resource."""
    action = "Would destroy" if dry_run else "Destroyed"
    if resource_type == "work_dir":
        return f"{action} work directory: {resource.path}"
    if resource_type == "machine":
        return f"{action} machine: {resource.host_name} ({resource.provider_name})"
    if resource_type == "machine_record":
        return f"{action} machine record: {resource.host_name} ({resource.provider_name})"
    if resource_type == "snapshot":
        return f"{action} snapshot: {resource.name}"
    if resource_type == "volume":
        return f"{action} volume: {resource.name}"
    if resource_type == "log":
        return f"{action} log: {resource.path}"
    if resource_type == "build_cache":
        return f"{action} build cache: {resource.path}"
    return f"{action} {resource_type}: {resource}"


def _emit_destroyed(
    resource_type: str,
    resource: Any,
    output_format: OutputFormat,
    dry_run: bool,
) -> None:
    """Emit a destroyed resource event."""
    # Emit event
    event_data = {
        "message": _format_destroyed_message(resource_type, resource, dry_run),
        "resource_type": resource_type,
        "resource": resource.model_dump(mode="json") if hasattr(resource, "model_dump") else str(resource),
        "dry_run": dry_run,
    }
    emit_event("destroyed", event_data, output_format)


def _emit_final_summary(result: GcResult, output_format: OutputFormat, dry_run: bool) -> None:
    """Emit the final summary for GC results."""
    match output_format:
        case OutputFormat.JSON:
            _emit_json_summary(result, dry_run)
        case OutputFormat.HUMAN:
            _emit_human_summary(result, dry_run)
        case OutputFormat.JSONL:
            _emit_jsonl_summary(result, dry_run)
        case _ as unreachable:
            assert_never(unreachable)


def _emit_json_summary(result: GcResult, dry_run: bool) -> None:
    """Emit JSON summary."""
    output_data = {
        "work_dirs_destroyed": [wd.model_dump(mode="json") for wd in result.work_dirs_destroyed],
        "machines_destroyed": [m.model_dump(mode="json") for m in result.machines_destroyed],
        "machines_deleted": [m.model_dump(mode="json") for m in result.machines_deleted],
        "snapshots_destroyed": [s.model_dump(mode="json") for s in result.snapshots_destroyed],
        "volumes_destroyed": [v.model_dump(mode="json") for v in result.volumes_destroyed],
        "logs_destroyed": [log.model_dump(mode="json") for log in result.logs_destroyed],
        "build_cache_destroyed": [cache.model_dump(mode="json") for cache in result.build_cache_destroyed],
        "errors": result.errors,
        "dry_run": dry_run,
    }
    emit_final_json(output_data)


def _emit_human_summary(result: GcResult, dry_run: bool) -> None:
    """Emit human-readable summary."""
    write_human_line("")
    if dry_run:
        write_human_line("Garbage Collection (Dry Run)")
    else:
        write_human_line("Garbage Collection Results")
    write_human_line("=" * 40)

    total_count = 0

    if result.work_dirs_destroyed:
        local_work_dirs = [wd for wd in result.work_dirs_destroyed if wd.is_local]
        local_count = len(local_work_dirs)
        local_size = sum(wd.size_bytes for wd in local_work_dirs)
        total_count_str = f"Work directories: {len(result.work_dirs_destroyed)}"
        if local_count > 0:
            total_count_str += f" ({local_count} local, freed {format_size(local_size)})"
        write_human_line("\n{}", total_count_str)
        total_count += len(result.work_dirs_destroyed)

    if result.machines_destroyed:
        write_human_line("\nMachines: {}", len(result.machines_destroyed))
        total_count += len(result.machines_destroyed)

    if result.machines_deleted:
        write_human_line("\nMachine records deleted: {}", len(result.machines_deleted))
        total_count += len(result.machines_deleted)

    if result.snapshots_destroyed:
        write_human_line("\nSnapshots: {}", len(result.snapshots_destroyed))
        total_count += len(result.snapshots_destroyed)

    if result.volumes_destroyed:
        write_human_line("\nVolumes: {}", len(result.volumes_destroyed))
        total_count += len(result.volumes_destroyed)

    if result.logs_destroyed:
        logs_size_bytes = sum(log.size_bytes for log in result.logs_destroyed)
        write_human_line("\nLogs: {} (freed {})", len(result.logs_destroyed), format_size(logs_size_bytes))
        total_count += len(result.logs_destroyed)

    if result.build_cache_destroyed:
        build_cache_size_bytes = sum(cache.size_bytes for cache in result.build_cache_destroyed)
        write_human_line(
            "\nBuild cache: {} (freed {})", len(result.build_cache_destroyed), format_size(build_cache_size_bytes)
        )
        total_count += len(result.build_cache_destroyed)

    if total_count == 0:
        write_human_line("\nNo resources found to destroy")
    else:
        action = "Would destroy" if dry_run else "Destroyed"
        write_human_line("\n{} {} resource(s) total", action, total_count)

    if result.errors:
        write_human_line("\nErrors:")
        for error in result.errors:
            write_human_line("  - {}", error)


def _emit_jsonl_summary(result: GcResult, dry_run: bool) -> None:
    """Emit JSONL summary event."""
    work_dirs_size_bytes = sum(wd.size_bytes for wd in result.work_dirs_destroyed)
    snapshots_size_bytes = sum(s.size_bytes for s in result.snapshots_destroyed if s.size_bytes is not None)
    volumes_size_bytes = sum(v.size_bytes for v in result.volumes_destroyed)
    logs_size_bytes = sum(log.size_bytes for log in result.logs_destroyed)
    build_cache_size_bytes = sum(cache.size_bytes for cache in result.build_cache_destroyed)
    total_size_bytes = (
        work_dirs_size_bytes + snapshots_size_bytes + volumes_size_bytes + logs_size_bytes + build_cache_size_bytes
    )
    total_count = (
        len(result.work_dirs_destroyed)
        + len(result.machines_destroyed)
        + len(result.machines_deleted)
        + len(result.snapshots_destroyed)
        + len(result.volumes_destroyed)
        + len(result.logs_destroyed)
        + len(result.build_cache_destroyed)
    )

    event = {
        "event": "summary",
        "total_count": total_count,
        "total_size_bytes": total_size_bytes,
        "work_dirs_count": len(result.work_dirs_destroyed),
        "machines_count": len(result.machines_destroyed),
        "machine_record_count": len(result.machines_deleted),
        "snapshots_count": len(result.snapshots_destroyed),
        "volumes_count": len(result.volumes_destroyed),
        "logs_count": len(result.logs_destroyed),
        "build_cache_count": len(result.build_cache_destroyed),
        "errors_count": len(result.errors),
        "errors": result.errors,
        "dry_run": dry_run,
    }
    emit_event("summary", event, OutputFormat.JSONL)


def _get_selected_providers(mng_ctx: MngContext, opts: GcCliOptions) -> list[ProviderInstanceInterface]:
    """Get providers based on CLI options."""
    if opts.all_providers:
        return list(get_all_provider_instances(mng_ctx))

    if opts.provider:
        providers = []
        for provider_name in opts.provider:
            providers.append(get_provider_instance(ProviderInstanceName(provider_name), mng_ctx))
        return providers

    return list(get_all_provider_instances(mng_ctx))


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="gc",
    one_line_description="Garbage collect unused resources",
    synopsis="mng gc [OPTIONS]",
    description="""Automatically removes containers, old snapshots, unused hosts, cached images,
and any resources that are associated with destroyed hosts and agents.

`mng destroy` automatically cleans up resources when an agent is deleted.
`mng gc` can be used to manually trigger garbage collection of unused
resources at any time.""",
    examples=(
        ("Preview what would be cleaned (dry run)", "mng gc --dry-run"),
        ("Clean all resources", "mng gc"),
        ("Clean resources for Docker only", "mng gc --provider docker"),
        ("Clean resources, continue on errors", "mng gc --on-error continue"),
    ),
    see_also=(
        ("cleanup", "Interactive cleanup of agents and hosts"),
        ("destroy", "Destroy agents (includes automatic GC)"),
        ("list", "List agents to find unused resources"),
    ),
).register()

# Add pager-enabled help option to the gc command
add_pager_help_option(gc)
