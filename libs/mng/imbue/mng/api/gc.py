import shlex
import shutil
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final
from typing import assert_never

from loguru import logger

from imbue.imbue_common.logging import log_call
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mng.api.data_types import GcResourceTypes
from imbue.mng.api.data_types import GcResult
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import HostAuthenticationError
from imbue.mng.errors import HostConnectionError
from imbue.mng.errors import HostOfflineError
from imbue.mng.errors import MngError
from imbue.mng.interfaces.data_types import BuildCacheInfo
from imbue.mng.interfaces.data_types import LogFileInfo
from imbue.mng.interfaces.data_types import SizeBytes
from imbue.mng.interfaces.data_types import WorkDirInfo
from imbue.mng.interfaces.host import HostInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import HostState
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.utils.git_utils import parse_worktree_git_file


@log_call
def gc(
    mng_ctx: MngContext,
    providers: Sequence[ProviderInstanceInterface],
    resource_types: GcResourceTypes,
    # If True, identify but don't destroy resources
    dry_run: bool,
    # Whether to abort or continue on errors
    error_behavior: ErrorBehavior,
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

    if resource_types.is_work_dirs:
        with log_span("Garbage collecting orphaned work directories"):
            gc_work_dirs(
                mng_ctx=mng_ctx,
                providers=providers,
                dry_run=dry_run,
                error_behavior=error_behavior,
                result=result,
            )

    if resource_types.is_machines:
        with log_span("Garbage collecting idle machines"):
            gc_machines(
                mng_ctx=mng_ctx,
                providers=providers,
                dry_run=dry_run,
                error_behavior=error_behavior,
                result=result,
            )

    if resource_types.is_snapshots:
        with log_span("Garbage collecting orphaned snapshots"):
            gc_snapshots(
                providers=providers,
                dry_run=dry_run,
                error_behavior=error_behavior,
                result=result,
            )

    if resource_types.is_volumes:
        with log_span("Garbage collecting orphaned volumes"):
            gc_volumes(
                providers=providers,
                dry_run=dry_run,
                error_behavior=error_behavior,
                result=result,
            )

    if resource_types.is_logs:
        with log_span("Garbage collecting old log files"):
            gc_logs(
                mng_ctx=mng_ctx,
                providers=providers,
                dry_run=dry_run,
                error_behavior=error_behavior,
                result=result,
            )

    if resource_types.is_build_cache:
        with log_span("Garbage collecting build cache entries"):
            gc_build_cache(
                mng_ctx=mng_ctx,
                providers=providers,
                dry_run=dry_run,
                error_behavior=error_behavior,
                result=result,
            )

    return result


def gc_work_dirs(
    mng_ctx: MngContext,
    providers: Sequence[ProviderInstanceInterface],
    dry_run: bool,
    error_behavior: ErrorBehavior,
    result: GcResult,
) -> None:
    """Garbage collect orphaned work directories."""
    for provider_instance in providers:
        for host_ref in provider_instance.discover_hosts(cg=mng_ctx.concurrency_group):
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
                    continue
                except HostAuthenticationError:
                    logger.trace("Skipped work dir GC because host authentication failed", host_id=host.id)
                    continue

                for work_dir_info in orphaned_dirs:
                    try:
                        if not dry_run:
                            _clean_work_dir(host=host, work_dir_path=work_dir_info.path, dry_run=False)
                        result.work_dirs_destroyed.append(work_dir_info)
                    except MngError as e:
                        error_msg = f"Failed to clean {work_dir_info.path}: {e}"
                        result.errors.append(error_msg)
                        _handle_error(error_msg, error_behavior, exc=e)


def gc_machines(
    mng_ctx: MngContext,
    providers: Sequence[ProviderInstanceInterface],
    dry_run: bool,
    error_behavior: ErrorBehavior,
    result: GcResult,
) -> None:
    """Garbage collect idle machines and delete old offline host records."""
    for provider in providers:
        try:
            host_refs = provider.discover_hosts(include_destroyed=True, cg=provider.mng_ctx.concurrency_group)

            for host_ref in host_refs:
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
                            if len(host.discover_agents()) == 0 or host.get_state() in (
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
                                    provider.delete_host(host)
                                result.machines_deleted.append(host_ref)
                        # no matter what we're done--the rest of the logic only applies to online hosts
                        continue

                    # Skip local hosts - they cannot be destroyed
                    if host.is_local:
                        continue

                    try:
                        # Only consider online hosts with no agents
                        agent_refs = host.discover_agents()
                        if len(agent_refs) > 0:
                            continue
                        host_to_destroy: HostInterface = host
                    except HostAuthenticationError:
                        # hosts that fail to authenticate should be destroyed--we assume all hosts are reachable
                        logger.warning("Failed to authenticate with host during GC, destroying: {}", host.id)
                        host_to_destroy = host.to_offline_host()
                    except HostConnectionError as e:
                        # we skip hosts that suddenly appear offline for now--it's hard to tell exactly what happened
                        logger.warning("Failed to connect to host {} during gc, skipping: {}", host.id, e)
                        continue

                    if not dry_run:
                        mng_ctx.pm.hook.on_before_host_destroy(host=host_to_destroy)
                        provider.destroy_host(host_to_destroy)
                        mng_ctx.pm.hook.on_host_destroyed(host=host_to_destroy)

                    result.machines_destroyed.append(host_ref)

                except MngError as e:
                    error_msg = f"Failed to check/destroy host {host_ref.host_id}: {e}"
                    result.errors.append(error_msg)
                    _handle_error(error_msg, error_behavior, exc=e)

        except MngError as e:
            error_msg = f"Failed to discover hosts for provider {provider.name}: {e}"
            result.errors.append(error_msg)
            _handle_error(error_msg, error_behavior, exc=e)


def gc_snapshots(
    providers: Sequence[ProviderInstanceInterface],
    dry_run: bool,
    error_behavior: ErrorBehavior,
    result: GcResult,
) -> None:
    """Garbage collect orphaned snapshots."""
    for provider in providers:
        if not provider.supports_snapshots:
            logger.trace("Skipped provider {} (does not support snapshots)", provider.name)
            continue

        try:
            host_refs = provider.discover_hosts(include_destroyed=False, cg=provider.mng_ctx.concurrency_group)

            for host_ref in host_refs:
                try:
                    snapshots = provider.list_snapshots(host_ref.host_id)

                    for snapshot in snapshots:
                        if not dry_run:
                            provider.delete_snapshot(host_ref.host_id, snapshot.id)

                        result.snapshots_destroyed.append(snapshot)

                except MngError as e:
                    error_msg = f"Failed to cleanup snapshots for host {host_ref.host_id}: {e}"
                    result.errors.append(error_msg)
                    _handle_error(error_msg, error_behavior, exc=e)

        except MngError as e:
            error_msg = f"Failed to process snapshots for provider {provider.name}: {e}"
            result.errors.append(error_msg)
            _handle_error(error_msg, error_behavior, exc=e)


def gc_volumes(
    providers: Sequence[ProviderInstanceInterface],
    dry_run: bool,
    error_behavior: ErrorBehavior,
    result: GcResult,
) -> None:
    """Garbage collect orphaned volumes."""
    for provider in providers:
        if not provider.supports_volumes:
            logger.trace("Skipped provider {} (does not support volumes)", provider.name)
            continue

        try:
            # Get all volumes
            all_volumes = provider.list_volumes()

            # Get volumes that are currently attached to hosts
            active_volume_ids = set()
            for host_ref in provider.discover_hosts(include_destroyed=False, cg=provider.mng_ctx.concurrency_group):
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

                except MngError as e:
                    error_msg = f"Failed to delete volume {volume.name}: {e}"
                    result.errors.append(error_msg)
                    _handle_error(error_msg, error_behavior, exc=e)

        except MngError as e:
            error_msg = f"Failed to process volumes for provider {provider.name}: {e}"
            result.errors.append(error_msg)
            _handle_error(error_msg, error_behavior, exc=e)


_LOG_MAX_AGE_DAYS: Final[int] = 30


def gc_logs(
    mng_ctx: MngContext,
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
    log_dir = mng_ctx.config.logging.log_dir
    if not log_dir.is_absolute():
        events_dir = mng_ctx.config.default_host_dir.expanduser() / log_dir
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

        except MngError as e:
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
    mng_ctx: MngContext,
    providers: Sequence[ProviderInstanceInterface],
    dry_run: bool,
    error_behavior: ErrorBehavior,
    result: GcResult,
) -> None:
    """Garbage collect build cache entries."""
    # Construct providers directory from profile
    base_cache_dir = mng_ctx.profile_dir / "providers"

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

            except MngError as e:
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
            result = host.execute_command(f"du -sb {shlex.quote(str(work_dir_path))} | cut -f1")
            if result.success and result.stdout.strip():
                size = SizeBytes(int(result.stdout.strip()))
        except (ValueError, OSError):
            # If we can't get the size, use 0
            pass

        # Get creation time from the directory
        created_at = datetime.now(timezone.utc)
        try:
            stat_result = host.execute_command(f"stat -c %Y {shlex.quote(str(work_dir_path))}")
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

    result = host.execute_command(f"test -f {shlex.quote(str(git_path))}")
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

    result = host.execute_command(cmd)

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


def _remove_directory(host: OnlineHostInterface, path: Path) -> None:
    """Remove a directory and all its contents."""
    result = host.execute_command(f"test -e {shlex.quote(str(path))}")
    if result.success:
        cmd = f"rm -rf {shlex.quote(str(path))}"
        result = host.execute_command(cmd)

        if not result.success:
            raise MngError(f"Failed to remove directory {path}: {result.stderr}")

        logger.debug("Removed directory: {}", path)


def _handle_error(error_msg: str, error_behavior: ErrorBehavior, exc: Exception | None = None) -> None:
    """Handle an error according to the specified error behavior."""
    match error_behavior:
        case ErrorBehavior.ABORT:
            if exc:
                raise exc
            raise MngError(error_msg)
        case ErrorBehavior.CONTINUE:
            if exc:
                logger.exception(exc)
            else:
                logger.error(error_msg)
        case _ as unreachable:
            assert_never(unreachable)
