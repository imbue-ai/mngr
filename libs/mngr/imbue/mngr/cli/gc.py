from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.api.data_types import GcResourceTypes
from imbue.mngr.api.data_types import GcResult
from imbue.mngr.api.gc import gc as api_gc
from imbue.mngr.api.providers import get_all_provider_instances
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.exit_codes import exit_code_for_failures
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import emit_info
from imbue.mngr.cli.output_helpers import format_size
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import ProviderEmptyError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.interfaces.data_types import CleanupFailure
from imbue.mngr.interfaces.data_types import CleanupFailureCategory
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import ProviderInstanceName


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
@add_common_options
@click.pass_context
def gc(ctx: click.Context, **kwargs) -> None:
    try:
        result = _gc_impl(ctx, **kwargs)
    except AbortError as e:
        # AbortError means we should exit immediately with an error
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)
        return
    # Exit with a cause-specific code derived from the structured failures (0 when none),
    # mirroring `mngr cleanup`/`destroy`/`stop`.
    ctx.exit(exit_code_for_failures(result.failures))


def _gc_impl(ctx: click.Context, **kwargs) -> GcResult:
    """Implementation of gc command (extracted for exception handling).

    Returns the aggregated GcResult so the caller can derive a cause-specific
    exit code from its structured failures.
    """
    # Setup command context (config, logging, output options)
    # This loads the config, applies defaults, and creates the final options
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="gc",
        command_class=GcCliOptions,
    )

    return _run_gc_iteration(mngr_ctx=mngr_ctx, opts=opts, output_opts=output_opts)


def _run_gc_iteration(mngr_ctx: MngrContext, opts: GcCliOptions, output_opts: OutputOptions) -> GcResult:
    """Run a single gc iteration.

    Returns the aggregated GcResult. Skipped explicitly-requested providers are
    recorded as PROVIDER_INACCESSIBLE failures, and per-resource cleanup
    failures are recorded with their cause-specific categories; the caller
    derives the process exit code from the full set of failures.
    """
    error_behavior = ErrorBehavior(opts.on_error.upper())

    providers, skipped_provider_errors = _get_selected_providers(mngr_ctx=mngr_ctx, opts=opts)

    # Always GC all resource types
    resource_types = GcResourceTypes(
        is_machines=True,
        is_snapshots=True,
        is_volumes=True,
        is_work_dirs=True,
        is_logs=True,
        is_build_cache=True,
        is_provider_resources=True,
    )

    # Call the API
    result = api_gc(
        mngr_ctx=mngr_ctx,
        providers=providers,
        resource_types=resource_types,
        dry_run=opts.dry_run,
        error_behavior=error_behavior,
        on_resource_type_start=lambda rt: _emit_resource_type_start(rt, output_opts.output_format),
    )

    # Surface explicitly-requested providers that were skipped (empty/unavailable)
    # as structured failures so the user sees them in the summary and the CLI exits
    # non-zero (PROVIDER_INACCESSIBLE, exit code 6).
    result.failures.extend(
        CleanupFailure(category=CleanupFailureCategory.PROVIDER_INACCESSIBLE, message=message)
        for message in skipped_provider_errors
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
    for provider_resource in result.provider_resources_destroyed:
        _emit_destroyed("provider_resource", provider_resource, output_opts.output_format, opts.dry_run)

    # Emit final summary
    _emit_final_summary(result=result, output_format=output_opts.output_format, dry_run=opts.dry_run)

    return result


_RESOURCE_TYPE_MESSAGES: dict[str, str] = {
    "work_dirs": "Cleaning work directories...",
    "machines": "Cleaning machines...",
    "snapshots": "Cleaning snapshots...",
    "volumes": "Cleaning volumes...",
    "logs": "Cleaning logs...",
    "build_cache": "Cleaning build cache...",
    "provider_resources": "Cleaning orphaned provider resources...",
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
    if resource_type == "provider_resource":
        return f"{action} {resource.kind}: {resource.name} ({resource.provider_name})"
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
        "provider_resources_destroyed": [pr.model_dump(mode="json") for pr in result.provider_resources_destroyed],
        "errors": result.errors,
        "failures": [failure.model_dump(mode="json") for failure in result.failures],
        "dry_run": dry_run,
    }
    write_json_line(output_data)


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

    if result.provider_resources_destroyed:
        write_human_line("\nProvider resources: {}", len(result.provider_resources_destroyed))
        total_count += len(result.provider_resources_destroyed)

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
        + len(result.provider_resources_destroyed)
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
        "provider_resources_count": len(result.provider_resources_destroyed),
        "errors_count": len(result.errors),
        "errors": result.errors,
        "failures": [failure.model_dump(mode="json") for failure in result.failures],
        "dry_run": dry_run,
    }
    emit_event("summary", event, OutputFormat.JSONL)


def _get_selected_providers(
    mngr_ctx: MngrContext, opts: GcCliOptions
) -> tuple[list[ProviderInstanceInterface], list[str]]:
    """Get providers based on CLI options.

    Returns the resolved providers and a list of error messages for any
    explicitly-requested providers that were skipped (empty or unavailable).
    Skipping lets gc still run against the providers that did resolve;
    surfacing the errors lets the caller exit non-zero so the user sees that
    their explicit request was not fully honored.
    """
    if opts.all_providers:
        return list(get_all_provider_instances(mngr_ctx)), []

    if opts.provider:
        providers = []
        skipped_errors: list[str] = []
        for provider_name in opts.provider:
            name = ProviderInstanceName(provider_name)
            # An explicitly-requested provider that is empty (e.g. a fresh
            # Modal per-user environment with nothing to collect) or
            # unavailable is still recorded as an error: the user asked us to
            # gc it specifically and we did not. Continue so the remaining
            # providers' work still happens; the CLI exits non-zero at the end.
            try:
                providers.append(get_provider_instance(name, mngr_ctx))
            except ProviderEmptyError as e:
                logger.debug("Skipping provider {} (empty -- nothing to gc): {}", name, e)
            except ProviderUnavailableError as e:
                logger.error("Skipping provider {} (unavailable): {}", name, e)
                skipped_errors.append(f"provider {name} is unavailable: {e}")
        return providers, skipped_errors

    return list(get_all_provider_instances(mngr_ctx)), []


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="gc",
    one_line_description="Garbage collect unused resources",
    synopsis="mngr gc [--all-providers] [--provider <PROVIDER>] [--dry-run] [--on-error <MODE>]",
    description="""Automatically removes containers, old snapshots, unused hosts, cached images,
and any resources that are associated with destroyed hosts and agents.

`mngr destroy` automatically cleans up resources when an agent is deleted.
`mngr gc` can be used to manually trigger garbage collection of unused
resources at any time.""",
    examples=(
        ("Preview what would be cleaned (dry run)", "mngr gc --dry-run"),
        ("Clean all resources", "mngr gc"),
        ("Clean resources for Docker only", "mngr gc --provider docker"),
        ("Clean resources, continue on errors", "mngr gc --on-error continue"),
    ),
    see_also=(
        ("cleanup", "Interactive cleanup of agents and hosts"),
        ("destroy", "Destroy agents (includes automatic GC)"),
        ("list", "List agents to find unused resources"),
    ),
).register()

# Add pager-enabled help option to the gc command
add_pager_help_option(gc)
