import shlex
import shutil
from collections.abc import Callable
from collections.abc import Sequence
from concurrent.futures import Future
from datetime import datetime
from datetime import timezone
from pathlib import Path
from threading import Lock
from typing import Final
from typing import assert_never

from loguru import logger

from imbue.imbue_common.logging import log_call
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mngr.api.data_types import GcResourceTypes
from imbue.mngr.api.data_types import GcResult
from imbue.mngr.api.discovery_events import emit_host_destroyed
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostAuthenticationError
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import HostOfflineError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.hosts.common import get_seconds_since_last_activity
from imbue.mngr.interfaces.data_types import BuildCacheInfo
from imbue.mngr.interfaces.data_types import LogFileInfo
from imbue.mngr.interfaces.data_types import SizeBytes
from imbue.mngr.interfaces.data_types import WorkDirInfo
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.git_utils import parse_worktree_git_file
from imbue.mngr.utils.thread_cleanup import mngr_executor


@log_call
def gc(
    mngr_ctx: MngrContext,
    providers: Sequence[ProviderInstanceInterface],
    resource_types: GcResourceTypes,
    # If True, identify but don't destroy resources
    dry_run: bool,
    # Whether to abort or continue on errors
    error_behavior: ErrorBehavior,
    # Called before each resource type is processed, with the resource type name
    on_resource_type_start: Callable[[str], None] | None = None,
) -> GcResult:
    """Run garbage collection on specified resources across providers.

    Identifies and optionally destroys unused resources including:
    - Orphaned work directories
    - Idle machines with no agents
    - Orphaned snapshots
    - Orphaned volumes
    - Old log files
    - Build cache entries
    """
    result = GcResult()
    logger.trace("Configured GC: dry_run={} error_behavior={}", dry_run, error_behavior)

    # Discover hosts once per provider and reuse across all GC functions.
    # This avoids repeated discover_hosts() calls that each retry failing
    # backends and emit duplicate warnings (e.g. when Docker is offline).
    all_hosts_by_provider = _discover_hosts_for_gc(providers, mngr_ctx)

    if resource_types.is_work_dirs:
        if on_resource_type_start:
            on_resource_type_start("work_dirs")
        with log_span("Garbage collecting orphaned work directories"):
            gc_work_dirs(
                mngr_ctx=mngr_ctx,
                hosts_by_provider=all_hosts_by_provider,
                dry_run=dry_run,
                error_behavior=error_behavior,
                result=result,
            )

    if resource_types.is_machines:
        if on_resource_type_start:
            on_resource_type_start("machines")
        with log_span("Garbage collecting idle machines"):
            gc_machines(
                mngr_ctx=mngr_ctx,
                hosts_by_provider=all_hosts_by_provider,
                dry_run=dry_run,
                error_behavior=error_behavior,
                result=result,
            )

    if resource_types.is_snapshots:
        if on_resource_type_start:
            on_resource_type_start("snapshots")
        with log_span("Garbage collecting orphaned snapshots"):
            gc_snapshots(
                hosts_by_provider=all_hosts_by_provider,
                dry_run=dry_run,
                error_behavior=error_behavior,
                result=result,
            )

    if resource_types.is_volumes:
        if on_resource_type_start:
            on_resource_type_start("volumes")
        with log_span("Garbage collecting orphaned volumes"):
            gc_volumes(
                hosts_by_provider=all_hosts_by_provider,
                dry_run=dry_run,
                error_behavior=error_behavior,
                result=result,
            )

    if resource_types.is_logs:
        if on_resource_type_start:
            on_resource_type_start("logs")
        with log_span("Garbage collecting old log files"):
            gc_logs(
                mngr_ctx=mngr_ctx,
                providers=providers,
                dry_run=dry_run,
                error_behavior=error_behavior,
                result=result,
            )

    if resource_types.is_build_cache:
        if on_resource_type_start:
            on_resource_type_start("build_cache")
        with log_span("Garbage collecting build cache entries"):
            gc_build_cache(
                mngr_ctx=mngr_ctx,
                providers=providers,
                dry_run=dry_run,
                error_behavior=error_behavior,
                result=result,
            )

    return result


ProviderHosts = list[tuple[ProviderInstanceInterface, list[DiscoveredHost]]]
"""A list of (provider, discovered_hosts) pairs for passing pre-computed discovery results."""


def _discover_hosts_for_gc(
    providers: Sequence[ProviderInstanceInterface],
    mngr_ctx: MngrContext,
) -> ProviderHosts:
    """Discover hosts from all providers, returning (provider, hosts) pairs.

    Uses include_destroyed=True so every GC function gets the complete picture.
    Functions that need only non-destroyed hosts can filter by host_state.

    Provider-level errors are caught and logged so that a single unavailable
    provider does not prevent GC from running on other providers.
    """
    result: ProviderHosts = []
    for provider in providers:
        try:
            hosts = provider.discover_hosts(
                include_destroyed=True,
                cg=mngr_ctx.concurrency_group,
            )
        except MngrError as e:
            logger.warning("Failed to discover hosts for provider {}: {}", provider.name, e)
            # Skip the provider entirely when discovery fails.  This is
            # critical for gc_volumes: if we recorded (provider, []) instead,
            # gc_volumes would call list_volumes() and treat *every* volume as
            # orphaned (no known hosts -> no active volumes -> delete all).
            continue
        result.append((provider, hosts))
    return result


def gc_work_dirs(
    mngr_ctx: MngrContext,
    hosts_by_provider: ProviderHosts,
    dry_run: bool,
    error_behavior: ErrorBehavior,
    result: GcResult,
) -> None:
    """Garbage collect orphaned work directories."""
    futures: list[Future[None]] = []
    with mngr_executor(parent_cg=mngr_ctx.concurrency_group, name="gc_machines", max_workers=32) as executor:
        for provider_instance, host_refs in hosts_by_provider:
            for host_ref in host_refs:
                if host_ref.host_state == HostState.DESTROYED:
                    continue
                futures.append(
                    executor.submit(
                        _gc_single_host_work_dir, host_ref, provider_instance, error_behavior, dry_run, result
                    )
                )
    # Re-raise any thread exceptions
    for future in futures:
        future.result()


def _gc_single_host_work_dir(
    host_ref: DiscoveredHost,
    provider_instance: ProviderInstanceInterface,
    error_behavior: ErrorBehavior,
    dry_run: bool,
    result: GcResult,
) -> None:
    host = provider_instance.get_host(host_ref.host_id)
    if not isinstance(host, OnlineHostInterface):
        # Skip offline hosts - can't query them
        logger.trace("Skipped work dir GC because host is offline", host_id=host.id)
    else:
        # otherwise is online
        try:
            orphaned_dirs = _get_orphaned_work_dirs(host=host, provider_name=provider_instance.name)
        except HostOfflineError:
            logger.trace("Skipped work dir GC because host is offline", host_id=host.id)
        except HostAuthenticationError:
            logger.trace("Skipped work dir GC because host authentication failed", host_id=host.id)
        else:
            for work_dir_info in orphaned_dirs:
                try:
                    if not dry_run:
                        _clean_work_dir(host=host, work_dir_path=work_dir_info.path, dry_run=False)
                    result.work_dirs_destroyed.append(work_dir_info)
                except MngrError as e:
                    error_msg = f"Failed to clean {work_dir_info.path}: {e}"
                    result.errors.append(error_msg)
                    _handle_error(error_msg, error_behavior, exc=e)

            # Source dirs (e.g. mngr-managed clones from --source <url>) are tracked
            # separately and kept around while any worktree still points at them.
            try:
                deletable_source_dirs, kept_source_dirs = _get_orphaned_source_dirs(
                    host=host, provider_name=provider_instance.name
                )
            except HostOfflineError:
                logger.trace("Skipped source dir GC because host is offline", host_id=host.id)
            except HostAuthenticationError:
                logger.trace("Skipped source dir GC because host authentication failed", host_id=host.id)
            else:
                for info in kept_source_dirs:
                    logger.warning(
                        "Keeping source repo {} because it has local branches not on any remote. "
                        "Push or delete them to allow future gc.",
                        info.path,
                    )
                result.source_dirs_kept_due_to_unpushed_branches.extend(kept_source_dirs)
                for source_dir_info in deletable_source_dirs:
                    try:
                        if not dry_run:
                            _clean_source_dir(host=host, source_dir_path=source_dir_info.path)
                        result.source_dirs_destroyed.append(source_dir_info)
                    except MngrError as e:
                        error_msg = f"Failed to clean source dir {source_dir_info.path}: {e}"
                        result.errors.append(error_msg)
                        _handle_error(error_msg, error_behavior, exc=e)


def gc_machines(
    mngr_ctx: MngrContext,
    hosts_by_provider: ProviderHosts,
    dry_run: bool,
    error_behavior: ErrorBehavior,
    result: GcResult,
) -> None:
    """Garbage collect idle machines and delete old offline host records."""
    results_lock = Lock()

    for provider, host_refs in hosts_by_provider:
        # Process hosts in parallel to avoid sequential SSH timeouts for offline hosts
        futures: list[Future[None]] = []
        with mngr_executor(parent_cg=mngr_ctx.concurrency_group, name="gc_machines", max_workers=32) as executor:
            for host_ref in host_refs:
                futures.append(
                    executor.submit(
                        _gc_single_host,
                        host_ref,
                        provider,
                        mngr_ctx,
                        dry_run,
                        error_behavior,
                        result,
                        results_lock,
                    )
                )

        # Re-raise any thread exceptions
        for future in futures:
            future.result()


def _gc_single_host(
    host_ref: DiscoveredHost,
    provider: ProviderInstanceInterface,
    mngr_ctx: MngrContext,
    dry_run: bool,
    error_behavior: ErrorBehavior,
    result: GcResult,
    results_lock: Lock,
) -> None:
    """Process a single host for garbage collection.

    This function is run in a thread by gc_machines.
    Results are merged into the shared result object under the results_lock.
    """
    try:
        host = provider.get_host(host_ref.host_id)

        # Handle offline hosts
        # all we care about is that they have no agents (or is failed/crashed/destroyed),
        # and that they're sufficiently old
        # if so, then we permanently delete the associated data (to prevent data from accumulating)
        if not isinstance(host, OnlineHostInterface):
            seconds_since_stopped = host.get_seconds_since_stopped()
            if (
                seconds_since_stopped is not None
                and seconds_since_stopped > provider.get_max_destroyed_host_persisted_seconds()
            ):
                agent_refs = host.discover_agents()
                if len(agent_refs) == 0 or host.get_state() in (
                    HostState.FAILED,
                    HostState.CRASHED,
                    HostState.DESTROYED,
                ):
                    # permanently delete the host's data
                    if not dry_run:
                        # FOLLOWUP: when there are multiple instance of gc running concurrently on different hosts
                        #  there's a risk of getting into a screwy situation here--if we delete this right as
                        #  someone else starts it, you might have a host that is running but is untracked
                        #  This can be easily fixed by adding some host-id-keyed locking at the provider level (which both create/start/delete would acquire)
                        provider.destroy_host(host)
                        emit_host_destroyed(
                            mngr_ctx.config,
                            host_ref.host_id,
                            [ref.agent_id for ref in agent_refs],
                        )
                    with results_lock:
                        result.machines_deleted.append(host_ref)
            # no matter what we're done--the rest of the logic only applies to online hosts
            return

        # Skip local hosts - they cannot be destroyed
        if host.is_local:
            return

        try:
            # Only consider online hosts with no agents
            agent_refs = host.discover_agents()
            if len(agent_refs) > 0:
                return
        except HostAuthenticationError as e:
            # Transient auth failures (network blip, infrastructure hiccup) must
            # not trigger destruction -- we cannot verify the host has no agents
            # or determine its age when we cannot authenticate.
            logger.warning("Failed to authenticate with host {} during GC, skipping: {}", host.id, e)
            return
        except HostConnectionError as e:
            # we skip hosts that suddenly appear offline for now--it's hard to tell exactly what happened
            logger.warning("Failed to connect to host {} during gc, skipping: {}", host.id, e)
            return

        # Only destroy hosts that have been quiet for long enough.  Young or
        # recently-touched hosts may be mid-setup, being debugged via SSH, or
        # otherwise in active use outside of mngr's view.
        try:
            seconds_since_activity = get_seconds_since_last_activity(host)
        except (HostAuthenticationError, HostConnectionError) as e:
            # Cannot determine activity -- err on the side of caution.  HostConnectionError
            # also catches its HostOfflineError subclass.
            logger.warning("Cannot determine last activity of host {} during GC, skipping: {}", host.id, e)
            return
        min_age_seconds = provider.get_min_online_host_age_seconds()
        if seconds_since_activity is not None and seconds_since_activity < min_age_seconds:
            logger.trace(
                "Skipped GC for host {} (last activity {:.0f}s ago < minimum {:.0f}s)",
                host.id,
                seconds_since_activity,
                min_age_seconds,
            )
            return
        if seconds_since_activity is None:
            # No activity recorded -- typically means the host crashed before
            # anything had a chance to write an activity file.  Fall back to
            # created_at for the setup grace period, and require a terminal
            # state so we don't destroy hosts that are still booting/healthy.
            try:
                certified_data = host.get_certified_data()
            except (HostAuthenticationError, HostConnectionError) as e:
                logger.warning("Cannot read certified data for host {} during GC, skipping: {}", host.id, e)
                return
            host_age_seconds = (datetime.now(timezone.utc) - certified_data.created_at).total_seconds()
            if host_age_seconds < min_age_seconds:
                logger.trace(
                    "Skipped GC for host {} (no activity, age {:.0f}s < minimum {:.0f}s)",
                    host.id,
                    host_age_seconds,
                    min_age_seconds,
                )
                return
            try:
                state = host.get_state()
            except (HostAuthenticationError, HostConnectionError) as e:
                logger.warning("Cannot determine state of host {} during GC, skipping: {}", host.id, e)
                return
            if state not in (HostState.CRASHED, HostState.FAILED):
                logger.trace(
                    "Skipped GC for host {} (no activity, past grace period, but state {} is not terminal)",
                    host.id,
                    state,
                )
                return
            # Past grace period, no activity, in terminal state -- fall through to destroy.

        if not dry_run:
            mngr_ctx.pm.hook.on_before_host_destroy(host=host, mngr_ctx=mngr_ctx)
            provider.destroy_host(host)
            mngr_ctx.pm.hook.on_host_destroyed(host=host, mngr_ctx=mngr_ctx)
            emit_host_destroyed(mngr_ctx.config, host_ref.host_id, [])

        with results_lock:
            result.machines_destroyed.append(host_ref)

    except MngrError as e:
        error_msg = f"Failed to check/destroy host {host_ref.host_id}: {e}"
        with results_lock:
            result.errors.append(error_msg)
        _handle_error(error_msg, error_behavior, exc=e)


def gc_snapshots(
    hosts_by_provider: ProviderHosts,
    dry_run: bool,
    error_behavior: ErrorBehavior,
    result: GcResult,
) -> None:
    """Garbage collect old snapshots from destroyed hosts.

    Only deletes snapshots from hosts that were in DESTROYED state at
    discovery time, and only after the snapshot exceeds the provider's
    ``destroyed_host_persisted_seconds`` threshold (default 7 days).
    Younger snapshots are preserved so users can recover via
    ``mngr create --snapshot``.

    Uses the host_state from the pre-computed discovery results rather than
    calling get_host(), because gc_machines may have already destroyed the
    host (and its record) earlier in the same GC run.

    Snapshots on RUNNING, PAUSED, and STOPPED hosts are never deleted:
    - PAUSED/STOPPED hosts need their snapshots for resumption
    - RUNNING hosts may have snapshots for backup/restore purposes
    """
    now = datetime.now(timezone.utc)
    for provider, host_refs in hosts_by_provider:
        if not provider.supports_snapshots:
            logger.trace("Skipped provider {} (does not support snapshots)", provider.name)
            continue

        destroyed_host_persisted_seconds = provider.get_max_destroyed_host_persisted_seconds()

        try:
            for host_ref in host_refs:
                try:
                    if host_ref.host_state != HostState.DESTROYED:
                        logger.trace(
                            "Skipped snapshot GC for host {} (state: {})",
                            host_ref.host_id,
                            host_ref.host_state,
                        )
                        continue

                    snapshots = provider.list_snapshots(host_ref.host_id)

                    for snapshot in snapshots:
                        snapshot_age_seconds = (now - snapshot.created_at).total_seconds()
                        if snapshot_age_seconds < destroyed_host_persisted_seconds:
                            logger.trace(
                                "Skipped snapshot {} on host {} (age {:.0f}s < threshold {:.0f}s)",
                                snapshot.id,
                                host_ref.host_id,
                                snapshot_age_seconds,
                                destroyed_host_persisted_seconds,
                            )
                            continue

                        if not dry_run:
                            provider.delete_snapshot(host_ref.host_id, snapshot.id)

                        result.snapshots_destroyed.append(snapshot)

                except MngrError as e:
                    error_msg = f"Failed to cleanup snapshots for host {host_ref.host_id}: {e}"
                    result.errors.append(error_msg)
                    _handle_error(error_msg, error_behavior, exc=e)

        except MngrError as e:
            error_msg = f"Failed to process snapshots for provider {provider.name}: {e}"
            result.errors.append(error_msg)
            _handle_error(error_msg, error_behavior, exc=e)


def gc_volumes(
    hosts_by_provider: ProviderHosts,
    dry_run: bool,
    error_behavior: ErrorBehavior,
    result: GcResult,
) -> None:
    """Garbage collect orphaned volumes."""
    for provider, host_refs in hosts_by_provider:
        if not provider.supports_volumes:
            logger.trace("Skipped provider {} (does not support volumes)", provider.name)
            continue

        try:
            # Get all volumes
            all_volumes = provider.list_volumes()

            # Get volumes that are currently attached to non-destroyed hosts.
            # Destroyed hosts' volumes are considered orphaned and should be cleaned up.
            active_volume_ids = set()
            for host_ref in host_refs:
                if host_ref.host_state == HostState.DESTROYED:
                    continue
                for volume in all_volumes:
                    if volume.host_id == host_ref.host_id:
                        active_volume_ids.add(volume.volume_id)

            # Identify orphaned volumes
            orphaned_volumes = [v for v in all_volumes if v.volume_id not in active_volume_ids]

            for volume in orphaned_volumes:
                try:
                    if not dry_run:
                        provider.delete_volume(volume.volume_id)

                    result.volumes_destroyed.append(volume)

                except MngrError as e:
                    error_msg = f"Failed to delete volume {volume.name}: {e}"
                    result.errors.append(error_msg)
                    _handle_error(error_msg, error_behavior, exc=e)

        except ProviderUnavailableError as e:
            # Provider is offline -- discover_hosts already warned the user.
            logger.debug("Skipped volume GC for provider {} (unavailable): {}", provider.name, e)
            continue
        except MngrError as e:
            error_msg = f"Failed to process volumes for provider {provider.name}: {e}"
            result.errors.append(error_msg)
            _handle_error(error_msg, error_behavior, exc=e)


_LOG_MAX_AGE_DAYS: Final[int] = 30


def gc_logs(
    mngr_ctx: MngrContext,
    providers: Sequence[ProviderInstanceInterface],
    dry_run: bool,
    error_behavior: ErrorBehavior,
    result: GcResult,
) -> None:
    """Garbage collect old rotated log files.

    Only targets the events/logs/ subdirectory (diagnostic logs), not the
    broader events/ directory which may contain non-log event data.

    Only deletes rotated log files (e.g., events.jsonl.1, events.jsonl.2)
    that are older than 30 days. The current log file (events.jsonl) is
    never deleted.
    """
    # Construct the logs subdirectory: events/logs/
    log_dir = mngr_ctx.config.logging.log_dir
    if not log_dir.is_absolute():
        events_dir = mngr_ctx.config.default_host_dir.expanduser() / log_dir
    else:
        events_dir = log_dir
    events_dir = events_dir.expanduser()

    # Only clean the logs/ subdirectory within events/
    logs_dir = events_dir / "logs"
    if not logs_dir.exists():
        logger.trace("Skipped logs directory {} (does not exist)", logs_dir)
        return

    now = datetime.now(timezone.utc)

    for log_file in logs_dir.rglob("*"):
        if not log_file.is_file():
            continue

        # Only delete rotated files (e.g., events.jsonl.1, events.jsonl.2).
        # Never delete the current log file (events.jsonl) or other non-rotated files.
        if not _is_rotated_log_file(log_file):
            continue

        try:
            stat = log_file.stat()
            file_size = SizeBytes(stat.st_size)
            modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

            # Only delete files older than the max age (based on last modification)
            age_days = (now - modified_at).days
            if age_days < _LOG_MAX_AGE_DAYS:
                logger.trace("Skipped log file {} (only {} days old)", log_file, age_days)
                continue

            log_file_info = LogFileInfo(path=log_file, size_bytes=file_size, created_at=modified_at)

            if not dry_run:
                log_file.unlink()

            result.logs_destroyed.append(log_file_info)

        except MngrError as e:
            error_msg = f"Failed to delete log {log_file}: {e}"
            result.errors.append(error_msg)
            _handle_error(error_msg, error_behavior, exc=e)


@pure
def _is_rotated_log_file(path: Path) -> bool:
    """Check if a file is a rotated log file (e.g., events.jsonl.1, events.jsonl.2).

    Rotated files are created by the JSONL file sink when the current log file
    exceeds max_size_bytes. They have a numeric suffix appended to the original
    filename (e.g., events.jsonl.1, events.jsonl.2).
    """
    name = path.name
    # Check for pattern: <basename>.<N> where N is a positive integer
    last_dot = name.rfind(".")
    if last_dot == -1:
        return False
    suffix = name[last_dot + 1 :]
    return suffix.isdigit()


def gc_build_cache(
    mngr_ctx: MngrContext,
    providers: Sequence[ProviderInstanceInterface],
    dry_run: bool,
    error_behavior: ErrorBehavior,
    result: GcResult,
) -> None:
    """Garbage collect build cache entries."""
    # Construct providers directory from profile
    base_cache_dir = mngr_ctx.profile_dir / "providers"

    if not base_cache_dir.exists():
        logger.trace("Skipped build cache directory {} (does not exist)", base_cache_dir)
        return

    for provider_dir in base_cache_dir.iterdir():
        if not provider_dir.is_dir():
            continue

        cache_dir = provider_dir / "cache"
        if not cache_dir.exists():
            continue

        # Clean up build cache entries
        for cache_entry in cache_dir.rglob("*"):
            if not cache_entry.is_dir():
                continue

            try:
                # Calculate size
                cache_entry_size = SizeBytes(sum(f.stat().st_size for f in cache_entry.rglob("*") if f.is_file()))
                # Get creation time
                created_at = datetime.fromtimestamp(cache_entry.stat().st_ctime, tz=timezone.utc)
                build_cache_info = BuildCacheInfo(path=cache_entry, size_bytes=cache_entry_size, created_at=created_at)

                if not dry_run:
                    # Remove the cache entry directory
                    shutil.rmtree(cache_entry)

                result.build_cache_destroyed.append(build_cache_info)

            except MngrError as e:
                error_msg = f"Failed to delete cache entry {cache_entry}: {e}"
                result.errors.append(error_msg)
                _handle_error(error_msg, error_behavior, exc=e)


def _get_orphaned_work_dirs(host: OnlineHostInterface, provider_name: ProviderInstanceName) -> list[WorkDirInfo]:
    """Get list of orphaned work directories for a host."""
    certified_data = host.get_certified_data()
    generated_work_dirs = set(certified_data.generated_work_dirs)

    active_work_dirs = set()
    for agent in host.get_agents():
        active_work_dirs.add(str(agent.work_dir))

    orphaned_work_dirs = generated_work_dirs - active_work_dirs

    work_dir_infos = []
    for work_dir_str in orphaned_work_dirs:
        work_dir_path = Path(work_dir_str)
        # Get size if possible
        size = SizeBytes(0)
        try:
            result = host.execute_idempotent_command(f"du -sb {shlex.quote(str(work_dir_path))} | cut -f1")
            if result.success and result.stdout.strip():
                size = SizeBytes(int(result.stdout.strip()))
        except (ValueError, OSError):
            # If we can't get the size, use 0
            pass

        # Get creation time from the directory
        created_at = datetime.now(timezone.utc)
        try:
            stat_result = host.execute_idempotent_command(f"stat -c %Y {shlex.quote(str(work_dir_path))}")
            if stat_result.success and stat_result.stdout.strip():
                created_at = datetime.fromtimestamp(int(stat_result.stdout.strip()), tz=timezone.utc)
        except (ValueError, OSError):
            pass

        work_dir_infos.append(
            WorkDirInfo(
                path=work_dir_path,
                size_bytes=size,
                host_id=host.id,
                provider_name=provider_name,
                is_local=host.is_local,
                created_at=created_at,
            )
        )

    return work_dir_infos


def _clean_work_dir(host: OnlineHostInterface, work_dir_path: Path, dry_run: bool) -> None:
    """Clean up a single work directory."""
    if not dry_run:
        with host.lock_cooperatively():
            if _is_git_worktree(host, work_dir_path):
                _remove_git_worktree(host, work_dir_path)
            else:
                _remove_directory(host, work_dir_path)

            _remove_work_dir_from_certified_data(host, work_dir_path)


def _is_git_worktree(host: OnlineHostInterface, path: Path) -> bool:
    """Check if a path is a git worktree.

    A git worktree has a .git file (not directory) that points to the main git directory.
    """
    git_path = path / ".git"

    result = host.execute_idempotent_command(f"test -f {shlex.quote(str(git_path))}")
    return result.success


def _remove_git_worktree(host: OnlineHostInterface, work_dir_path: Path) -> None:
    """Remove a git worktree using git worktree remove.

    Reads the .git file to find the main repo and runs the removal from there,
    which is required for git to properly unregister the worktree.
    """
    main_repo: Path | None = None
    git_file = work_dir_path / ".git"
    try:
        content = host.read_text_file(git_file)
        main_repo = parse_worktree_git_file(content)
    except (FileNotFoundError, OSError):
        pass

    if main_repo is not None:
        cmd = f"git -C {shlex.quote(str(main_repo))} worktree remove --force {shlex.quote(str(work_dir_path))}"
    else:
        cmd = f"git worktree remove --force {shlex.quote(str(work_dir_path))}"

    result = host.execute_idempotent_command(cmd)

    if not result.success:
        logger.warning("git worktree remove failed, falling back to directory removal: {}", result.stderr)
        _remove_directory(host, work_dir_path)
    else:
        logger.debug("Removed git worktree: {}", work_dir_path)


def _remove_work_dir_from_certified_data(host: OnlineHostInterface, work_dir_path: Path) -> None:
    """Remove a work directory from the host's certified data."""
    certified_data = host.get_certified_data()
    existing_dirs = set(certified_data.generated_work_dirs)
    existing_dirs.discard(str(work_dir_path))

    updated_data = certified_data.model_copy_update(
        to_update(certified_data.field_ref().generated_work_dirs, tuple(sorted(existing_dirs))),
    )

    host.set_certified_data(updated_data)


def register_generated_source_dir(host: OnlineHostInterface, source_dir: Path) -> None:
    """Record `source_dir` as an mngr-managed source repo on `host` so GC can clean it later."""
    certified_data = host.get_certified_data()
    existing_dirs = set(certified_data.generated_source_dirs)
    existing_dirs.add(str(source_dir))
    updated_data = certified_data.model_copy_update(
        to_update(certified_data.field_ref().generated_source_dirs, tuple(sorted(existing_dirs))),
    )
    host.set_certified_data(updated_data)


def _find_source_repo_of_worktree_on_host(host: OnlineHostInterface, worktree_path: Path) -> Path | None:
    """Host-aware counterpart to git_utils.find_source_repo_of_worktree.

    Reads the worktree's .git file through host.read_text_file so this works for
    both local and remote hosts. Returns None if the path is not a worktree or
    the .git file cannot be read.
    """
    try:
        content = host.read_text_file(worktree_path / ".git")
    except (FileNotFoundError, OSError):
        return None
    return parse_worktree_git_file(content)


def _get_orphaned_source_dirs(
    host: OnlineHostInterface, provider_name: ProviderInstanceName
) -> tuple[list[WorkDirInfo], list[WorkDirInfo]]:
    """Partition mngr-tracked source repos into (safe-to-delete, kept-due-to-unpushed-branches).

    A source repo is "in use" if a living agent's work_dir either is the source itself
    or is a git worktree backed by it. Anything else is orphan; an orphan with no local
    branches outside every remote is safe to delete.
    """
    certified_data = host.get_certified_data()
    source_dirs = set(certified_data.generated_source_dirs)
    if not source_dirs:
        return [], []

    in_use_sources: set[str] = set()
    for agent in host.get_agents():
        work_dir_str = str(agent.work_dir)
        if work_dir_str in source_dirs:
            in_use_sources.add(work_dir_str)
            continue
        source_of_worktree = _find_source_repo_of_worktree_on_host(host, agent.work_dir)
        if source_of_worktree is not None and str(source_of_worktree) in source_dirs:
            in_use_sources.add(str(source_of_worktree))

    deletable: list[WorkDirInfo] = []
    kept: list[WorkDirInfo] = []
    for source_dir_str in sorted(source_dirs - in_use_sources):
        source_path = Path(source_dir_str)
        info = _build_source_dir_info(host, provider_name, source_path)
        unpushed = _local_branches_not_on_any_remote_on_host(host, source_path)
        if unpushed:
            logger.debug("Source {} has unpushed branches: {}", source_path, unpushed)
            kept.append(info)
        else:
            deletable.append(info)
    return deletable, kept


_BRANCH_LISTING_FAILED_SENTINEL: Final[str] = "<branch listing failed>"


def _local_branches_not_on_any_remote_on_host(host: OnlineHostInterface, repo_path: Path) -> list[str]:
    """Return local branches in repo_path whose tip is not contained in any remote ref.

    Runs git via host.execute_idempotent_command so this works for both local and remote
    hosts. Failure is treated as "possibly unpushed" to stay on the safe side: we return
    a non-empty list containing a sentinel so the caller keeps the repo instead of
    deleting it.
    """
    quoted_path = shlex.quote(str(repo_path))
    list_result = host.execute_idempotent_command(
        f"git -C {quoted_path} for-each-ref --format={shlex.quote('%(refname:short)')} refs/heads/"
    )
    if not list_result.success:
        logger.warning(
            "Failed to list local branches in {} ({}); treating as possibly-unpushed to avoid data loss.",
            repo_path,
            list_result.stderr.strip(),
        )
        return [_BRANCH_LISTING_FAILED_SENTINEL]

    unpushed: list[str] = []
    for branch in list_result.stdout.splitlines():
        branch = branch.strip()
        if not branch:
            continue
        contains_result = host.execute_idempotent_command(
            f"git -C {quoted_path} branch -r --contains {shlex.quote(branch)}"
        )
        if not contains_result.success or not contains_result.stdout.strip():
            unpushed.append(branch)
    return unpushed


def _build_source_dir_info(
    host: OnlineHostInterface, provider_name: ProviderInstanceName, source_path: Path
) -> WorkDirInfo:
    size = SizeBytes(0)
    try:
        result = host.execute_idempotent_command(f"du -sb {shlex.quote(str(source_path))} | cut -f1")
        if result.success and result.stdout.strip():
            size = SizeBytes(int(result.stdout.strip()))
    except (ValueError, OSError):
        pass

    created_at = datetime.now(timezone.utc)
    try:
        stat_result = host.execute_idempotent_command(f"stat -c %Y {shlex.quote(str(source_path))}")
        if stat_result.success and stat_result.stdout.strip():
            created_at = datetime.fromtimestamp(int(stat_result.stdout.strip()), tz=timezone.utc)
    except (ValueError, OSError):
        pass

    return WorkDirInfo(
        path=source_path,
        size_bytes=size,
        host_id=host.id,
        provider_name=provider_name,
        is_local=host.is_local,
        created_at=created_at,
    )


def _clean_source_dir(host: OnlineHostInterface, source_dir_path: Path) -> None:
    """Remove a managed source repo and drop it from the host's certified data."""
    with host.lock_cooperatively():
        _remove_directory(host, source_dir_path)
        certified_data = host.get_certified_data()
        existing_dirs = set(certified_data.generated_source_dirs)
        existing_dirs.discard(str(source_dir_path))
        updated_data = certified_data.model_copy_update(
            to_update(certified_data.field_ref().generated_source_dirs, tuple(sorted(existing_dirs))),
        )
        host.set_certified_data(updated_data)


def _remove_directory(host: OnlineHostInterface, path: Path) -> None:
    """Remove a directory and all its contents.

    Tries without sudo first, then retries with sudo if the initial
    attempt fails (e.g. on Lima VMs where the SSH user is not root but
    has passwordless sudo).
    """
    result = host.execute_idempotent_command(f"test -e {shlex.quote(str(path))}")
    if result.success:
        quoted = shlex.quote(str(path))
        result = host.execute_idempotent_command(f"rm -rf {quoted}")

        if not result.success:
            logger.debug("rm -rf failed for {}, retrying with sudo: {}", path, result.stderr)
            result = host.execute_idempotent_command(f"sudo rm -rf {quoted}")

        if not result.success:
            raise MngrError(f"Failed to remove directory {path}: {result.stderr}")

        logger.debug("Removed directory: {}", path)


def _handle_error(error_msg: str, error_behavior: ErrorBehavior, exc: Exception | None = None) -> None:
    """Handle an error according to the specified error behavior."""
    match error_behavior:
        case ErrorBehavior.ABORT:
            if exc:
                raise exc
            raise MngrError(error_msg)
        case ErrorBehavior.CONTINUE:
            if exc:
                logger.opt(exception=exc).error(error_msg)
            else:
                logger.error(error_msg)
        case _ as unreachable:
            assert_never(unreachable)
