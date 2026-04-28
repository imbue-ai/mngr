"""Unit tests for gc API functions."""

import os
import subprocess
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pluggy
import pytest
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.data_types import GcResourceTypes
from imbue.mngr.api.data_types import GcResult
from imbue.mngr.api.gc import ProviderHosts
from imbue.mngr.api.gc import _LOG_MAX_AGE_DAYS
from imbue.mngr.api.gc import _clean_work_dir
from imbue.mngr.api.gc import _discover_hosts_for_gc
from imbue.mngr.api.gc import _gc_single_host_work_dir
from imbue.mngr.api.gc import _get_orphaned_source_dirs
from imbue.mngr.api.gc import _get_orphaned_work_dirs
from imbue.mngr.api.gc import _handle_error
from imbue.mngr.api.gc import _is_git_worktree
from imbue.mngr.api.gc import _is_rotated_log_file
from imbue.mngr.api.gc import _local_branches_not_on_any_remote_on_host
from imbue.mngr.api.gc import _remove_directory
from imbue.mngr.api.gc import _remove_git_worktree
from imbue.mngr.api.gc import _remove_work_dir_from_certified_data
from imbue.mngr.api.gc import gc
from imbue.mngr.api.gc import gc_build_cache
from imbue.mngr.api.gc import gc_logs
from imbue.mngr.api.gc import gc_machines
from imbue.mngr.api.gc import gc_snapshots
from imbue.mngr.api.gc import gc_volumes
from imbue.mngr.api.gc import gc_work_dirs
from imbue.mngr.api.gc import register_generated_source_dir
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import _DEFAULT_MIN_ONLINE_HOST_AGE_SECONDS
from imbue.mngr.errors import HostAuthenticationError
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import HostOfflineError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.interfaces.data_types import VolumeInfo
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import VolumeId
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.providers.mock_provider_test import MockProviderInstance
from imbue.mngr.providers.mock_provider_test import make_offline_host
from imbue.mngr.utils.logging import LoggingConfig
from imbue.mngr.utils.testing import make_mngr_ctx


def _hosts_for(provider: BaseProviderInstance) -> ProviderHosts:
    """Discover hosts from a provider and return as a ProviderHosts list."""
    return [(provider, provider.discover_hosts(include_destroyed=True, cg=provider.mngr_ctx.concurrency_group))]


def test_gc_machines_skips_local_hosts(local_provider: LocalProviderInstance, temp_mngr_ctx: MngrContext) -> None:
    """Test that gc_machines skips local hosts even when they have no agents."""
    result = GcResult()

    gc_machines(
        mngr_ctx=temp_mngr_ctx,
        hosts_by_provider=_hosts_for(local_provider),
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    # Local host should be skipped, not destroyed
    assert len(result.machines_destroyed) == 0
    assert len(result.errors) == 0


# =========================================================================
# gc_machines offline host deletion tests
# =========================================================================


def _make_offline_host(
    provider: MockProviderInstance,
    mngr_ctx: MngrContext,
    *,
    days_old: int = 14,
    stop_reason: str | None = HostState.STOPPED.value,
    failure_reason: str | None = None,
) -> OfflineHost:
    """Create an offline host with configurable age and state."""
    stopped_at = datetime.now(timezone.utc) - timedelta(days=days_old)
    certified_data = CertifiedHostData(
        host_id=str(HostId.generate()),
        host_name="test-host",
        stop_reason=stop_reason,
        failure_reason=failure_reason,
        created_at=stopped_at - timedelta(hours=1),
        updated_at=stopped_at,
    )
    return make_offline_host(certified_data, provider, mngr_ctx)


def _run_gc_machines(provider: MockProviderInstance, *, dry_run: bool = False) -> GcResult:
    """Run gc_machines on a single provider and return the result."""
    result = GcResult()
    gc_machines(
        mngr_ctx=provider.mngr_ctx,
        hosts_by_provider=_hosts_for(provider),
        dry_run=dry_run,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )
    return result


def test_gc_machines_deletes_old_offline_host_with_no_agents(
    gc_mock_provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """Old offline hosts with no agents are deleted to prevent data accumulation."""
    host = _make_offline_host(gc_mock_provider, temp_mngr_ctx, days_old=14)
    gc_mock_provider.mock_hosts = [host]

    result = _run_gc_machines(gc_mock_provider)

    assert len(result.machines_deleted) == 1
    assert result.machines_deleted[0].host_id == host.id
    assert gc_mock_provider.deleted_hosts == [host.id]


def test_gc_machines_skips_recent_offline_host(
    gc_mock_provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """Offline hosts stopped less than the max persisted seconds ago are not deleted."""
    host = _make_offline_host(gc_mock_provider, temp_mngr_ctx, days_old=1)
    gc_mock_provider.mock_hosts = [host]

    result = _run_gc_machines(gc_mock_provider)

    assert len(result.machines_deleted) == 0
    assert gc_mock_provider.deleted_hosts == []


def _add_mock_agent(provider: MockProviderInstance) -> None:
    """Add a mock agent to the provider so hosts appear to have agents."""
    agent_id = AgentId.generate()
    provider.mock_agent_data = [{"id": str(agent_id), "name": "test-agent"}]


def test_gc_machines_deletes_old_crashed_host_with_agents(
    gc_mock_provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """Old offline hosts in CRASHED state are deleted even if they have agents."""
    # None stop_reason means the host CRASHED
    host = _make_offline_host(gc_mock_provider, temp_mngr_ctx, stop_reason=None)
    _add_mock_agent(gc_mock_provider)
    gc_mock_provider.mock_hosts = [host]

    result = _run_gc_machines(gc_mock_provider)

    assert len(result.machines_deleted) == 1
    assert gc_mock_provider.deleted_hosts == [host.id]


def test_gc_machines_skips_old_stopped_host_with_agents(
    gc_mock_provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """Old offline hosts in STOPPED state with agents are not deleted."""
    host = _make_offline_host(gc_mock_provider, temp_mngr_ctx, days_old=14)
    _add_mock_agent(gc_mock_provider)
    gc_mock_provider.mock_hosts = [host]

    result = _run_gc_machines(gc_mock_provider)

    assert len(result.machines_deleted) == 0
    assert gc_mock_provider.deleted_hosts == []


def test_gc_machines_dry_run_does_not_call_delete_host(
    gc_mock_provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """Dry run identifies hosts for deletion but does not actually delete them."""
    host = _make_offline_host(gc_mock_provider, temp_mngr_ctx, days_old=14)
    gc_mock_provider.mock_hosts = [host]

    result = _run_gc_machines(gc_mock_provider, dry_run=True)

    assert len(result.machines_deleted) == 1
    assert gc_mock_provider.deleted_hosts == []


def test_gc_machines_deletes_old_failed_host_with_agents(
    gc_mock_provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """Old offline hosts in FAILED state are deleted even if they have agents."""
    host = _make_offline_host(gc_mock_provider, temp_mngr_ctx, failure_reason="Build failed")
    _add_mock_agent(gc_mock_provider)
    gc_mock_provider.mock_hosts = [host]

    result = _run_gc_machines(gc_mock_provider)

    assert len(result.machines_deleted) == 1
    assert gc_mock_provider.deleted_hosts == [host.id]


def test_gc_machines_deletes_old_destroyed_host_with_agents(
    gc_mock_provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """Old offline hosts in DESTROYED state are deleted even if they have agents."""
    host = _make_offline_host(gc_mock_provider, temp_mngr_ctx)
    # Make the provider not support snapshots and not support shutdown hosts
    # so the state resolves to DESTROYED
    gc_mock_provider.mock_supports_snapshots = False
    gc_mock_provider.mock_supports_shutdown_hosts = False
    _add_mock_agent(gc_mock_provider)
    gc_mock_provider.mock_hosts = [host]

    result = _run_gc_machines(gc_mock_provider)

    assert len(result.machines_deleted) == 1
    assert gc_mock_provider.deleted_hosts == [host.id]


# =========================================================================
# _handle_error tests
# =========================================================================


def test_handle_error_abort_raises_provided_exception() -> None:
    """ABORT behavior re-raises the provided exception."""
    exc = MngrError("test error")
    with pytest.raises(MngrError, match="test error"):
        _handle_error("some message", ErrorBehavior.ABORT, exc=exc)


def test_handle_error_abort_raises_mngr_error_when_no_exception() -> None:
    """ABORT behavior raises MngrError from the message when no exception is provided."""
    with pytest.raises(MngrError, match="some message"):
        _handle_error("some message", ErrorBehavior.ABORT, exc=None)


@pytest.mark.allow_warnings(match=r"^(test|some message)$")
def test_handle_error_continue_does_not_raise() -> None:
    """CONTINUE behavior logs instead of raising."""
    # Should not raise
    _handle_error("some message", ErrorBehavior.CONTINUE, exc=MngrError("test"))
    _handle_error("some message", ErrorBehavior.CONTINUE, exc=None)


# =========================================================================
# gc_logs tests
# =========================================================================


def _make_old(path: Path, days: int) -> None:
    """Set a file's mtime to be `days` old by backdating atime/mtime."""
    old_time = time.time() - (days * 86400)
    os.utime(path, (old_time, old_time))


def test_gc_logs_deletes_old_rotated_files(temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance) -> None:
    """gc_logs deletes rotated log files older than 30 days under events/logs/."""
    events_dir = temp_mngr_ctx.config.default_host_dir.expanduser() / temp_mngr_ctx.config.logging.log_dir
    logs_dir = events_dir / "logs" / "mngr"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Create a rotated log file and make it old
    rotated = logs_dir / "events.jsonl.1"
    rotated.write_text("old log content")
    _make_old(rotated, _LOG_MAX_AGE_DAYS + 1)

    result = GcResult()
    gc_logs(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.logs_destroyed) == 1
    assert not rotated.exists()


def test_gc_logs_preserves_current_log_file(temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance) -> None:
    """gc_logs never deletes the current log file (events.jsonl)."""
    events_dir = temp_mngr_ctx.config.default_host_dir.expanduser() / temp_mngr_ctx.config.logging.log_dir
    logs_dir = events_dir / "logs" / "mngr"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Create the current log file and make it old
    current = logs_dir / "events.jsonl"
    current.write_text("current log content")
    _make_old(current, _LOG_MAX_AGE_DAYS + 10)

    result = GcResult()
    gc_logs(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.logs_destroyed) == 0
    assert current.exists()


def test_gc_logs_preserves_recent_rotated_files(
    temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance
) -> None:
    """gc_logs preserves rotated files that are younger than 30 days."""
    events_dir = temp_mngr_ctx.config.default_host_dir.expanduser() / temp_mngr_ctx.config.logging.log_dir
    logs_dir = events_dir / "logs" / "mngr"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Create a recent rotated file (should be preserved)
    recent = logs_dir / "events.jsonl.1"
    recent.write_text("recent rotated content")
    # Don't backdate -- it's brand new

    result = GcResult()
    gc_logs(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.logs_destroyed) == 0
    assert recent.exists()


def test_gc_logs_dry_run_does_not_delete(temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance) -> None:
    """In dry_run mode, gc_logs identifies files but does not delete them."""
    events_dir = temp_mngr_ctx.config.default_host_dir.expanduser() / temp_mngr_ctx.config.logging.log_dir
    logs_dir = events_dir / "logs" / "mngr"
    logs_dir.mkdir(parents=True, exist_ok=True)

    rotated = logs_dir / "events.jsonl.1"
    rotated.write_text("log content")
    _make_old(rotated, _LOG_MAX_AGE_DAYS + 1)

    result = GcResult()
    gc_logs(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        dry_run=True,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.logs_destroyed) == 1
    assert rotated.exists()


def test_gc_logs_skips_nonexistent_directory(
    temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance
) -> None:
    """gc_logs returns early when the logs directory does not exist."""
    result = GcResult()
    gc_logs(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.logs_destroyed) == 0


def test_gc_logs_does_not_touch_event_files_outside_logs(
    temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance
) -> None:
    """gc_logs only targets events/logs/, not other directories under events/."""
    events_dir = temp_mngr_ctx.config.default_host_dir.expanduser() / temp_mngr_ctx.config.logging.log_dir

    # Create a non-log event file directly under events/
    conversations_dir = events_dir / "conversations"
    conversations_dir.mkdir(parents=True, exist_ok=True)
    event_file = conversations_dir / "events.jsonl.1"
    event_file.write_text("conversation event")
    _make_old(event_file, _LOG_MAX_AGE_DAYS + 10)

    result = GcResult()
    gc_logs(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.logs_destroyed) == 0
    assert event_file.exists()


def test_gc_logs_populates_log_file_info(temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance) -> None:
    """gc_logs populates LogFileInfo with correct path, size, and creation time."""
    events_dir = temp_mngr_ctx.config.default_host_dir.expanduser() / temp_mngr_ctx.config.logging.log_dir
    logs_dir = events_dir / "logs" / "mngr"
    logs_dir.mkdir(parents=True, exist_ok=True)

    content = "some log content here"
    rotated = logs_dir / "events.jsonl.1"
    rotated.write_text(content)
    _make_old(rotated, _LOG_MAX_AGE_DAYS + 5)

    result = GcResult()
    gc_logs(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        dry_run=True,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.logs_destroyed) == 1
    info = result.logs_destroyed[0]
    assert info.path == rotated
    assert info.size_bytes == len(content)
    assert info.created_at is not None


# -- _is_rotated_log_file tests --


def test_is_rotated_log_file_matches_numeric_suffix() -> None:
    assert _is_rotated_log_file(Path("events.jsonl.1")) is True
    assert _is_rotated_log_file(Path("events.jsonl.42")) is True


def test_is_rotated_log_file_rejects_current_log() -> None:
    assert _is_rotated_log_file(Path("events.jsonl")) is False


def test_is_rotated_log_file_rejects_non_numeric_suffix() -> None:
    assert _is_rotated_log_file(Path("events.jsonl.bak")) is False
    assert _is_rotated_log_file(Path("events.jsonl.tmp")) is False


def test_is_rotated_log_file_rejects_other_files() -> None:
    assert _is_rotated_log_file(Path("data.json")) is False
    assert _is_rotated_log_file(Path("noextension")) is False


# =========================================================================
# gc_build_cache tests
# =========================================================================


def test_gc_build_cache_finds_and_deletes_cache_entries(
    temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance
) -> None:
    """gc_build_cache finds cache entry directories and deletes them."""
    cache_dir = temp_mngr_ctx.profile_dir / "providers" / "some-provider" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Create cache entries with files inside
    entry1 = cache_dir / "entry-1"
    entry1.mkdir()
    (entry1 / "layer.tar").write_text("layer data")

    entry2 = cache_dir / "entry-2"
    entry2.mkdir()
    (entry2 / "manifest.json").write_text("{}")

    result = GcResult()
    gc_build_cache(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.build_cache_destroyed) >= 2
    # Directories should be deleted
    assert not entry1.exists()
    assert not entry2.exists()


def test_gc_build_cache_dry_run_does_not_delete(
    temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance
) -> None:
    """In dry_run mode, gc_build_cache identifies entries but does not delete them."""
    cache_dir = temp_mngr_ctx.profile_dir / "providers" / "some-provider" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    entry = cache_dir / "entry-1"
    entry.mkdir()
    (entry / "data.bin").write_text("binary data")

    result = GcResult()
    gc_build_cache(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        dry_run=True,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.build_cache_destroyed) >= 1
    # Directory should still exist
    assert entry.exists()


def test_gc_build_cache_skips_nonexistent_directory(
    temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance
) -> None:
    """gc_build_cache returns early when the providers directory does not exist."""
    result = GcResult()
    gc_build_cache(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.build_cache_destroyed) == 0


def test_gc_build_cache_skips_provider_without_cache_dir(
    temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance
) -> None:
    """gc_build_cache skips provider directories that have no cache subdirectory."""
    providers_dir = temp_mngr_ctx.profile_dir / "providers" / "some-provider"
    providers_dir.mkdir(parents=True, exist_ok=True)
    # No "cache" subdirectory created

    result = GcResult()
    gc_build_cache(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.build_cache_destroyed) == 0


def test_gc_build_cache_populates_build_cache_info(
    temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance
) -> None:
    """gc_build_cache populates BuildCacheInfo with correct path, size, and creation time."""
    cache_dir = temp_mngr_ctx.profile_dir / "providers" / "test-provider" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    entry = cache_dir / "entry-1"
    entry.mkdir()
    content = "some cached data"
    (entry / "file.bin").write_text(content)

    result = GcResult()
    gc_build_cache(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        dry_run=True,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.build_cache_destroyed) >= 1
    # Find the entry for our top-level cache entry
    entry_info = [info for info in result.build_cache_destroyed if info.path == entry]
    assert len(entry_info) == 1
    assert entry_info[0].size_bytes >= len(content)
    assert entry_info[0].created_at is not None


# =========================================================================
# gc() main function tests
# =========================================================================


def test_gc_with_only_logs_flag(temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance) -> None:
    """gc() only collects logs when is_logs=True and other flags are False."""
    events_dir = temp_mngr_ctx.config.default_host_dir.expanduser() / temp_mngr_ctx.config.logging.log_dir
    logs_dir = events_dir / "logs" / "mngr"
    logs_dir.mkdir(parents=True, exist_ok=True)
    rotated = logs_dir / "events.jsonl.1"
    rotated.write_text("content")
    _make_old(rotated, _LOG_MAX_AGE_DAYS + 1)

    resource_types = GcResourceTypes(is_logs=True)
    result = gc(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        resource_types=resource_types,
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )

    assert len(result.logs_destroyed) == 1
    assert len(result.machines_destroyed) == 0
    assert len(result.build_cache_destroyed) == 0


def test_gc_with_only_build_cache_flag(temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance) -> None:
    """gc() only collects build cache when is_build_cache=True and other flags are False."""
    cache_dir = temp_mngr_ctx.profile_dir / "providers" / "prov" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    entry = cache_dir / "entry-1"
    entry.mkdir()
    (entry / "data").write_text("data")

    resource_types = GcResourceTypes(is_build_cache=True)
    result = gc(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        resource_types=resource_types,
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )

    assert len(result.build_cache_destroyed) >= 1
    assert len(result.machines_destroyed) == 0
    assert len(result.logs_destroyed) == 0


def test_gc_with_no_flags_does_nothing(temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance) -> None:
    """gc() does nothing when all resource type flags are False."""
    resource_types = GcResourceTypes()
    result = gc(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        resource_types=resource_types,
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )

    assert len(result.logs_destroyed) == 0
    assert len(result.machines_destroyed) == 0
    assert len(result.machines_deleted) == 0
    assert len(result.build_cache_destroyed) == 0
    assert len(result.snapshots_destroyed) == 0
    assert len(result.volumes_destroyed) == 0
    assert len(result.work_dirs_destroyed) == 0


def test_gc_with_multiple_flags(temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance) -> None:
    """gc() collects both logs and build cache when both flags are set."""
    # Set up logs
    events_dir = temp_mngr_ctx.config.default_host_dir.expanduser() / temp_mngr_ctx.config.logging.log_dir
    logs_dir = events_dir / "logs" / "mngr"
    logs_dir.mkdir(parents=True, exist_ok=True)
    rotated = logs_dir / "events.jsonl.1"
    rotated.write_text("content")
    _make_old(rotated, _LOG_MAX_AGE_DAYS + 1)

    # Set up build cache
    cache_dir = temp_mngr_ctx.profile_dir / "providers" / "prov" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    entry = cache_dir / "entry-1"
    entry.mkdir()
    (entry / "data").write_text("data")

    resource_types = GcResourceTypes(is_logs=True, is_build_cache=True)
    result = gc(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        resource_types=resource_types,
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )

    assert len(result.logs_destroyed) == 1
    assert len(result.build_cache_destroyed) >= 1


# =========================================================================
# gc_work_dirs tests
# =========================================================================


def test_gc_work_dirs_no_orphans_on_fresh_provider(
    temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance
) -> None:
    """gc_work_dirs should find no orphaned work dirs on a fresh local provider."""
    result = GcResult()
    gc_work_dirs(
        mngr_ctx=temp_mngr_ctx,
        hosts_by_provider=_hosts_for(local_provider),
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )
    assert len(result.work_dirs_destroyed) == 0


# =========================================================================
# gc_snapshots tests
# =========================================================================


def test_gc_snapshots_skips_provider_without_snapshot_support(
    temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance
) -> None:
    """gc_snapshots should skip providers that do not support snapshots."""
    result = GcResult()
    gc_snapshots(
        hosts_by_provider=_hosts_for(local_provider),
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )
    assert len(result.snapshots_destroyed) == 0
    assert len(result.errors) == 0


def _make_snapshot_info(
    snapshot_id: str = "snap-001",
    name: str = "test-snapshot",
    created_at: datetime | None = None,
) -> SnapshotInfo:
    """Create a SnapshotInfo for testing."""
    return SnapshotInfo(
        id=SnapshotId(snapshot_id),
        name=SnapshotName(name),
        created_at=created_at if created_at is not None else datetime.now(timezone.utc),
    )


@pytest.mark.parametrize("stop_reason", [HostState.PAUSED.value, HostState.STOPPED.value])
def test_gc_snapshots_preserves_non_destroyed_host_snapshots(
    gc_mock_provider: MockProviderInstance, temp_mngr_ctx: MngrContext, stop_reason: str
) -> None:
    """gc_snapshots must not delete snapshots from PAUSED or STOPPED hosts.

    These hosts need their snapshots for resumption. Deleting them would
    cause the host to be considered DESTROYED.
    """
    host = _make_offline_host(gc_mock_provider, temp_mngr_ctx, stop_reason=stop_reason)
    gc_mock_provider.mock_hosts = [host]
    gc_mock_provider.mock_snapshots = [_make_snapshot_info()]

    result = GcResult()
    gc_snapshots(
        hosts_by_provider=_hosts_for(gc_mock_provider),
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.snapshots_destroyed) == 0
    assert gc_mock_provider.deleted_snapshots == []


def test_gc_snapshots_preserves_paused_host_snapshots_snapshot_based_provider(
    gc_mock_provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """gc_snapshots preserves snapshots on providers that use snapshots for resumption.

    This mimics the Modal provider scenario where supports_shutdown_hosts=False
    and the host relies on snapshots to determine its state. If gc deleted the
    snapshots, the host state would flip from PAUSED to DESTROYED.
    """
    gc_mock_provider.mock_supports_shutdown_hosts = False
    host = _make_offline_host(gc_mock_provider, temp_mngr_ctx, stop_reason=HostState.PAUSED.value)
    gc_mock_provider.mock_hosts = [host]
    gc_mock_provider.mock_snapshots = [_make_snapshot_info()]

    # Verify the host is PAUSED (not DESTROYED) before gc
    assert host.get_state() == HostState.PAUSED

    result = GcResult()
    gc_snapshots(
        hosts_by_provider=_hosts_for(gc_mock_provider),
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.snapshots_destroyed) == 0
    assert gc_mock_provider.deleted_snapshots == []
    # Verify the host is still PAUSED after gc
    assert host.get_state() == HostState.PAUSED


def test_gc_snapshots_deletes_old_destroyed_host_snapshots(
    gc_mock_provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """gc_snapshots deletes old snapshots from DESTROYED hosts.

    When a host is destroyed, its snapshots are kept for recovery until they
    exceed the destroyed_host_persisted_seconds threshold.
    """
    # supports_shutdown_hosts=True makes get_state() return the stop_reason directly,
    # so setting stop_reason=DESTROYED gives us a DESTROYED host on a provider that
    # still supports snapshots (so gc_snapshots doesn't skip the provider).
    host = _make_offline_host(gc_mock_provider, temp_mngr_ctx, stop_reason=HostState.DESTROYED.value)
    gc_mock_provider.mock_hosts = [host]

    old_created_at = datetime.now(timezone.utc) - timedelta(days=31)
    snapshot = _make_snapshot_info(created_at=old_created_at)
    gc_mock_provider.mock_snapshots = [snapshot]

    assert host.get_state() == HostState.DESTROYED

    result = GcResult()
    gc_snapshots(
        hosts_by_provider=_hosts_for(gc_mock_provider),
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.snapshots_destroyed) == 1
    assert result.snapshots_destroyed[0].id == snapshot.id
    assert len(gc_mock_provider.deleted_snapshots) == 1
    assert gc_mock_provider.deleted_snapshots[0] == (host.id, snapshot.id)


def test_gc_snapshots_preserves_young_destroyed_host_snapshots(
    gc_mock_provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """gc_snapshots preserves recent snapshots on DESTROYED hosts for recovery."""
    host = _make_offline_host(gc_mock_provider, temp_mngr_ctx, stop_reason=HostState.DESTROYED.value)
    gc_mock_provider.mock_hosts = [host]

    # Snapshot created recently -- should be preserved
    snapshot = _make_snapshot_info()
    gc_mock_provider.mock_snapshots = [snapshot]

    assert host.get_state() == HostState.DESTROYED

    result = GcResult()
    gc_snapshots(
        hosts_by_provider=_hosts_for(gc_mock_provider),
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.snapshots_destroyed) == 0
    assert gc_mock_provider.deleted_snapshots == []


def test_gc_snapshots_respects_custom_snapshot_age(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """gc_snapshots uses the provider's configured destroyed_host_persisted_seconds.

    With a short age (1 hour), a 2-hour-old snapshot on a DESTROYED host should be deleted.
    """
    # Configure provider with a 1-hour retention
    config = MngrConfig(
        prefix=temp_mngr_ctx.config.prefix,
        default_destroyed_host_persisted_seconds=3600.0,
    )
    ctx = temp_mngr_ctx.model_copy_update(
        to_update(temp_mngr_ctx.field_ref().config, config),
    )
    provider = MockProviderInstance(
        name=ProviderInstanceName("custom-age-provider"),
        host_dir=temp_host_dir,
        mngr_ctx=ctx,
    )

    host = _make_offline_host(provider, ctx, stop_reason=HostState.DESTROYED.value, days_old=1)
    provider.mock_hosts = [host]

    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    snapshot = _make_snapshot_info(created_at=two_hours_ago)
    provider.mock_snapshots = [snapshot]

    result = GcResult()
    gc_snapshots(
        hosts_by_provider=_hosts_for(provider),
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.snapshots_destroyed) == 1
    assert provider.deleted_snapshots == [(host.id, snapshot.id)]


# =========================================================================
# gc_volumes tests
# =========================================================================


def test_gc_volumes_skips_provider_without_volume_support(
    temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance
) -> None:
    """gc_volumes should skip providers that do not support volumes."""
    result = GcResult()
    gc_volumes(
        hosts_by_provider=_hosts_for(local_provider),
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )
    assert len(result.volumes_destroyed) == 0
    assert len(result.errors) == 0


def test_gc_volumes_does_not_delete_when_no_hosts_discovered(
    temp_mngr_ctx: MngrContext,
    temp_host_dir: Path,
) -> None:
    """gc_volumes must not delete volumes when the provider is present but has no hosts.

    Regression test: if _discover_hosts_for_gc recorded (provider, []) on
    discovery failure, gc_volumes would see zero active hosts and treat every
    volume as orphaned, deleting them all. The fix is to skip the provider
    entirely (not include it in hosts_by_provider) when discovery fails.
    """
    provider = MockProviderInstance(
        name=ProviderInstanceName("test-volume-provider"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
        mock_supports_volumes=True,
        mock_volumes=[
            VolumeInfo(
                volume_id=VolumeId("vol-00000000000000000000000000000001"),
                name="vol-00000000000000000000000000000001",
                size_bytes=0,
                host_id=HostId("host-00000000000000000000000000000001"),
            ),
        ],
    )

    # Simulate what would happen if the provider were included with an empty
    # host list (the dangerous case this test guards against):
    result = GcResult()
    gc_volumes(
        hosts_by_provider=[(provider, [])],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    # The volume should be deleted because there are no hosts to claim it.
    # This is correct when the provider is ONLINE with genuinely zero hosts.
    assert len(result.volumes_destroyed) == 1

    # But when the provider is OFFLINE (discovery failed), _discover_hosts_for_gc
    # must not include it at all. Verify that skipping the provider preserves volumes:
    provider.deleted_volumes.clear()
    result2 = GcResult()
    gc_volumes(
        hosts_by_provider=[],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result2,
    )
    assert len(result2.volumes_destroyed) == 0
    assert provider.deleted_volumes == []


# =========================================================================
# gc() with work_dirs, snapshots, volumes flags
# =========================================================================


def test_gc_with_work_dirs_flag(temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance) -> None:
    """gc() with is_work_dirs=True should run work directory garbage collection."""
    resource_types = GcResourceTypes(is_work_dirs=True)
    result = gc(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        resource_types=resource_types,
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )
    assert len(result.work_dirs_destroyed) == 0


def test_gc_with_snapshots_flag(temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance) -> None:
    """gc() with is_snapshots=True should run snapshot garbage collection."""
    resource_types = GcResourceTypes(is_snapshots=True)
    result = gc(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        resource_types=resource_types,
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )
    assert len(result.snapshots_destroyed) == 0


def test_gc_with_volumes_flag(temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance) -> None:
    """gc() with is_volumes=True should run volume garbage collection."""
    resource_types = GcResourceTypes(is_volumes=True)
    result = gc(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        resource_types=resource_types,
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )
    assert len(result.volumes_destroyed) == 0


def test_gc_with_machines_flag(temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance) -> None:
    """gc() with is_machines=True should run machine garbage collection."""
    resource_types = GcResourceTypes(is_machines=True)
    result = gc(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        resource_types=resource_types,
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )
    assert len(result.machines_destroyed) == 0


# =========================================================================
# _discover_hosts_for_gc error handling tests
# =========================================================================


class _DiscoveryErrorProvider(MockProviderInstance):
    """MockProviderInstance that raises MngrError from discover_hosts."""

    def discover_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list:
        raise MngrError("simulated discovery failure from test")


@pytest.mark.allow_warnings(
    match=r"^Failed\ to\ discover\ hosts\ for\ provider\ error\-provider:\ simulated\ discovery\ failure\ from\ test"
)
def test_discover_hosts_for_gc_skips_provider_on_error(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """_discover_hosts_for_gc skips a provider entirely when discovery raises MngrError.

    This is critical for gc_volumes: including the provider with an empty
    host list would incorrectly treat all its volumes as orphaned.
    """
    error_provider = _DiscoveryErrorProvider(
        name=ProviderInstanceName("error-provider"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )

    result = _discover_hosts_for_gc([error_provider], temp_mngr_ctx)

    # The failing provider must be absent from the result.
    assert result == []


@pytest.mark.allow_warnings(
    match=r"^Failed\ to\ discover\ hosts\ for\ provider\ error\-provider:\ simulated\ discovery\ failure\ from\ test"
)
def test_discover_hosts_for_gc_continues_after_one_provider_fails(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    """_discover_hosts_for_gc continues with other providers after one fails."""
    error_provider = _DiscoveryErrorProvider(
        name=ProviderInstanceName("error-provider"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )

    # A normal provider with no hosts - discovery succeeds and returns []
    ok_provider = MockProviderInstance(
        name=ProviderInstanceName("ok-provider"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )

    result = _discover_hosts_for_gc([error_provider, ok_provider], temp_mngr_ctx)

    # Only the ok_provider entry should appear.
    assert len(result) == 1
    assert result[0][0] is ok_provider


# =========================================================================
# gc_work_dirs: DESTROYED host skip test
# =========================================================================


def test_gc_work_dirs_skips_destroyed_hosts(
    gc_mock_provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """gc_work_dirs skips hosts that are in DESTROYED state."""
    host = _make_offline_host(gc_mock_provider, temp_mngr_ctx, stop_reason=HostState.DESTROYED.value)
    gc_mock_provider.mock_hosts = [host]

    result = GcResult()
    gc_work_dirs(
        mngr_ctx=temp_mngr_ctx,
        hosts_by_provider=_hosts_for(gc_mock_provider),
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.work_dirs_destroyed) == 0
    assert len(result.errors) == 0


# =========================================================================
# _get_orphaned_work_dirs tests using local host
# =========================================================================


def test_get_orphaned_work_dirs_returns_empty_when_no_generated_dirs(
    local_host: Host, local_provider: LocalProviderInstance
) -> None:
    """_get_orphaned_work_dirs returns an empty list when no work dirs were generated."""
    orphaned = _get_orphaned_work_dirs(host=local_host, provider_name=local_provider.name)
    assert orphaned == []


def test_get_orphaned_work_dirs_reports_dir_with_no_active_agent(
    local_host: Host, local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """_get_orphaned_work_dirs returns work dirs not used by any active agent."""
    work_dir = tmp_path / "orphaned_work_dir"
    work_dir.mkdir()

    # Register the work dir in certified data (simulating mngr having created it).
    certified = local_host.get_certified_data()
    updated = certified.model_copy_update(
        to_update(certified.field_ref().generated_work_dirs, (str(work_dir),)),
    )
    local_host.set_certified_data(updated)

    orphaned = _get_orphaned_work_dirs(host=local_host, provider_name=local_provider.name)

    assert len(orphaned) == 1
    assert orphaned[0].path == work_dir
    assert orphaned[0].host_id == local_host.id
    assert orphaned[0].provider_name == local_provider.name


def test_get_orphaned_work_dirs_handles_size_command_failure(
    local_host: Host, local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """_get_orphaned_work_dirs uses size 0 when the du command fails."""
    # Use a path that does not exist so du returns non-zero.
    nonexistent = tmp_path / "nonexistent_work_dir"

    certified = local_host.get_certified_data()
    updated = certified.model_copy_update(
        to_update(certified.field_ref().generated_work_dirs, (str(nonexistent),)),
    )
    local_host.set_certified_data(updated)

    orphaned = _get_orphaned_work_dirs(host=local_host, provider_name=local_provider.name)

    assert len(orphaned) == 1
    # Size defaults to 0 when the directory does not exist.
    assert orphaned[0].size_bytes == 0


# =========================================================================
# _is_git_worktree tests
# =========================================================================


def test_is_git_worktree_returns_true_for_worktree(local_host: Host, temp_git_repo: Path, tmp_path: Path) -> None:
    """_is_git_worktree returns True for a real git worktree."""
    wt_path = tmp_path / "worktree"
    subprocess.run(
        ["git", "-C", str(temp_git_repo), "worktree", "add", str(wt_path)],
        check=True,
        capture_output=True,
    )

    assert _is_git_worktree(local_host, wt_path) is True


def test_is_git_worktree_returns_false_for_plain_directory(local_host: Host, tmp_path: Path) -> None:
    """_is_git_worktree returns False when there is no .git file."""
    work_dir = tmp_path / "plain"
    work_dir.mkdir()

    assert _is_git_worktree(local_host, work_dir) is False


def test_is_git_worktree_returns_false_for_git_directory(local_host: Host, temp_git_repo: Path) -> None:
    """_is_git_worktree returns False for a real git repo (main repo, not worktree)."""
    assert _is_git_worktree(local_host, temp_git_repo) is False


# =========================================================================
# _remove_directory tests
# =========================================================================


def test_remove_directory_removes_existing_directory(local_host: Host, tmp_path: Path) -> None:
    """_remove_directory removes an existing directory and its contents."""
    target = tmp_path / "to_remove"
    target.mkdir()
    (target / "file.txt").write_text("content")

    _remove_directory(local_host, target)

    assert not target.exists()


def test_remove_directory_is_noop_for_nonexistent_path(local_host: Host, tmp_path: Path) -> None:
    """_remove_directory silently skips paths that do not exist."""
    nonexistent = tmp_path / "does_not_exist"

    # Should not raise
    _remove_directory(local_host, nonexistent)


# =========================================================================
# _remove_work_dir_from_certified_data tests
# =========================================================================


def test_remove_work_dir_from_certified_data_removes_entry(local_host: Host, tmp_path: Path) -> None:
    """_remove_work_dir_from_certified_data removes the work dir from certified data."""
    work_dir = tmp_path / "my_work_dir"
    work_dir.mkdir()

    certified = local_host.get_certified_data()
    updated = certified.model_copy_update(
        to_update(certified.field_ref().generated_work_dirs, (str(work_dir),)),
    )
    local_host.set_certified_data(updated)

    _remove_work_dir_from_certified_data(local_host, work_dir)

    new_certified = local_host.get_certified_data()
    assert str(work_dir) not in new_certified.generated_work_dirs


def test_remove_work_dir_from_certified_data_is_idempotent(local_host: Host, tmp_path: Path) -> None:
    """_remove_work_dir_from_certified_data is idempotent: removing absent dirs is safe."""
    work_dir = tmp_path / "absent_dir"

    # Should not raise even though the dir is not registered.
    _remove_work_dir_from_certified_data(local_host, work_dir)

    certified = local_host.get_certified_data()
    assert str(work_dir) not in certified.generated_work_dirs


# =========================================================================
# _clean_work_dir tests
# =========================================================================


def test_clean_work_dir_removes_plain_directory(local_host: Host, tmp_path: Path) -> None:
    """_clean_work_dir removes a plain (non-worktree) directory."""
    work_dir = tmp_path / "plain_work_dir"
    work_dir.mkdir()
    (work_dir / "file.txt").write_text("content")

    certified = local_host.get_certified_data()
    updated = certified.model_copy_update(
        to_update(certified.field_ref().generated_work_dirs, (str(work_dir),)),
    )
    local_host.set_certified_data(updated)

    _clean_work_dir(host=local_host, work_dir_path=work_dir, dry_run=False)

    assert not work_dir.exists()
    new_certified = local_host.get_certified_data()
    assert str(work_dir) not in new_certified.generated_work_dirs


def test_clean_work_dir_is_noop_in_dry_run(local_host: Host, tmp_path: Path) -> None:
    """_clean_work_dir does nothing in dry_run mode."""
    work_dir = tmp_path / "dry_run_work_dir"
    work_dir.mkdir()
    (work_dir / "file.txt").write_text("content")

    certified = local_host.get_certified_data()
    updated = certified.model_copy_update(
        to_update(certified.field_ref().generated_work_dirs, (str(work_dir),)),
    )
    local_host.set_certified_data(updated)

    _clean_work_dir(host=local_host, work_dir_path=work_dir, dry_run=True)

    # dry_run=True means _clean_work_dir returns immediately without doing anything.
    assert work_dir.exists()


def test_clean_work_dir_removes_git_worktree(local_host: Host, temp_git_repo: Path, tmp_path: Path) -> None:
    """_clean_work_dir uses git worktree remove for git worktrees."""
    wt_path = tmp_path / "my_worktree"
    subprocess.run(
        ["git", "-C", str(temp_git_repo), "worktree", "add", str(wt_path)],
        check=True,
        capture_output=True,
    )

    # Verify .git file (not directory) exists at worktree path.
    assert (wt_path / ".git").is_file()

    certified = local_host.get_certified_data()
    updated = certified.model_copy_update(
        to_update(certified.field_ref().generated_work_dirs, (str(wt_path),)),
    )
    local_host.set_certified_data(updated)

    _clean_work_dir(host=local_host, work_dir_path=wt_path, dry_run=False)

    assert not wt_path.exists()
    new_certified = local_host.get_certified_data()
    assert str(wt_path) not in new_certified.generated_work_dirs


# =========================================================================
# _remove_git_worktree tests
# =========================================================================


@pytest.mark.allow_warnings(
    match=r"^git\ worktree\ remove\ failed,\ falling\ back\ to\ directory\ removal:\ fatal:\ not\ a\ git\ repository\ \(or\ any\ of\ the\ parent\ directories\):\ \.git"
)
def test_remove_git_worktree_without_parseable_git_file_falls_back_to_rm(local_host: Host, tmp_path: Path) -> None:
    """_remove_git_worktree falls back to rm -rf when the .git file cannot be parsed."""
    work_dir = tmp_path / "pseudo_worktree"
    work_dir.mkdir()
    # Write a .git file with unparseable content so parse_worktree_git_file returns None.
    (work_dir / ".git").write_text("not a valid gitdir line")
    (work_dir / "some_file.py").write_text("code")

    _remove_git_worktree(local_host, work_dir)

    # The directory should have been removed via rm -rf fallback.
    assert not work_dir.exists()


@pytest.mark.allow_warnings(
    match=r"^git\ worktree\ remove\ failed,\ falling\ back\ to\ directory\ removal:\ fatal:\ cannot\ change\ to\ '"
)
def test_remove_git_worktree_falls_back_to_rm_when_git_not_in_main_repo(local_host: Host, tmp_path: Path) -> None:
    """_remove_git_worktree falls back to rm -rf when git worktree remove fails."""
    work_dir = tmp_path / "pseudo_worktree2"
    work_dir.mkdir()
    # Point to a non-existent main repo so git worktree remove fails.
    (work_dir / ".git").write_text("gitdir: /nonexistent/repo/.git/worktrees/abc\n")
    (work_dir / "some_file.py").write_text("code")

    _remove_git_worktree(local_host, work_dir)

    # The directory should have been removed via rm -rf fallback.
    assert not work_dir.exists()


# =========================================================================
# gc_work_dirs: real work dir cleanup on local host
# =========================================================================


def test_gc_work_dirs_destroys_orphaned_dir_on_local_host(
    local_host: Host,
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """gc_work_dirs removes orphaned work dirs on an online host."""
    work_dir = tmp_path / "orphaned"
    work_dir.mkdir()
    (work_dir / "file.txt").write_text("content")

    certified = local_host.get_certified_data()
    updated = certified.model_copy_update(
        to_update(certified.field_ref().generated_work_dirs, (str(work_dir),)),
    )
    local_host.set_certified_data(updated)

    result = GcResult()
    gc_work_dirs(
        mngr_ctx=temp_mngr_ctx,
        hosts_by_provider=_hosts_for(local_provider),
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.work_dirs_destroyed) == 1
    assert result.work_dirs_destroyed[0].path == work_dir
    assert not work_dir.exists()


def test_gc_work_dirs_dry_run_reports_but_does_not_delete(
    local_host: Host,
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """gc_work_dirs in dry_run mode reports orphaned dirs without deleting them."""
    work_dir = tmp_path / "orphaned_dry"
    work_dir.mkdir()

    certified = local_host.get_certified_data()
    updated = certified.model_copy_update(
        to_update(certified.field_ref().generated_work_dirs, (str(work_dir),)),
    )
    local_host.set_certified_data(updated)

    result = GcResult()
    gc_work_dirs(
        mngr_ctx=temp_mngr_ctx,
        hosts_by_provider=_hosts_for(local_provider),
        dry_run=True,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.work_dirs_destroyed) == 1
    assert work_dir.exists()


# =========================================================================
# _gc_single_host_work_dir error path tests
# =========================================================================


class _HostOfflineErrorProvider(MockProviderInstance):
    """Provider that returns the first host in mock_hosts from get_host."""

    def get_host(self, host: HostId | HostName) -> HostInterface:
        if self.mock_hosts:
            return self.mock_hosts[0]
        return super().get_host(host)


class _OfflineErroringHost(Host):
    """Host subclass whose get_certified_data raises HostOfflineError."""

    def get_certified_data(self) -> CertifiedHostData:
        raise HostOfflineError("simulated offline error from test")


class _AuthErroringHost(Host):
    """Host subclass whose get_certified_data raises HostAuthenticationError."""

    def get_certified_data(self) -> CertifiedHostData:
        raise HostAuthenticationError("simulated auth error from test")


def _make_erroring_host(provider: LocalProviderInstance, host_cls: type[Host]) -> Host:
    """Create an instance of host_cls using the local provider's connector and ID."""
    pyinfra_host = provider._create_local_pyinfra_host()
    connector = PyinfraConnector(pyinfra_host)
    return host_cls(
        id=provider.host_id,
        connector=connector,
        provider_instance=provider,
        mngr_ctx=provider.mngr_ctx,
    )


def test_gc_single_host_work_dir_skips_host_offline_error(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_host_dir: Path,
) -> None:
    """_gc_single_host_work_dir skips hosts that raise HostOfflineError."""
    erroring_host = _make_erroring_host(local_provider, _OfflineErroringHost)

    provider = _HostOfflineErrorProvider(
        name=ProviderInstanceName("test-offline"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
        mock_hosts=[erroring_host],
    )

    host_ref = DiscoveredHost(
        host_id=erroring_host.id,
        host_name=erroring_host.get_name(),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )

    result = GcResult()
    _gc_single_host_work_dir(host_ref, provider, ErrorBehavior.ABORT, False, result)

    assert len(result.work_dirs_destroyed) == 0
    assert len(result.errors) == 0


def test_gc_single_host_work_dir_skips_host_auth_error(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_host_dir: Path,
) -> None:
    """_gc_single_host_work_dir skips hosts that raise HostAuthenticationError."""
    erroring_host = _make_erroring_host(local_provider, _AuthErroringHost)

    provider = _HostOfflineErrorProvider(
        name=ProviderInstanceName("test-auth"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
        mock_hosts=[erroring_host],
    )

    host_ref = DiscoveredHost(
        host_id=erroring_host.id,
        host_name=erroring_host.get_name(),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )

    result = GcResult()
    _gc_single_host_work_dir(host_ref, provider, ErrorBehavior.ABORT, False, result)

    assert len(result.work_dirs_destroyed) == 0
    assert len(result.errors) == 0


# =========================================================================
# gc_machines: outer MngrError handler test
# =========================================================================


class _GetHostErrorProvider(MockProviderInstance):
    """Provider that raises MngrError from get_host."""

    def get_host(self, host: HostId | HostName) -> HostInterface:
        raise MngrError("simulated get_host failure from test")


@pytest.mark.allow_warnings(match=r"^simulated\ get_host\ failure\ from\ test")
def test_gc_machines_handles_mngr_error_with_continue(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """gc_machines catches MngrError per-host when ErrorBehavior.CONTINUE is set."""
    host = _make_offline_host(
        MockProviderInstance(
            name=ProviderInstanceName("dummy"),
            host_dir=temp_host_dir,
            mngr_ctx=temp_mngr_ctx,
        ),
        temp_mngr_ctx,
        days_old=14,
    )

    error_provider = _GetHostErrorProvider(
        name=ProviderInstanceName("error-provider"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
        mock_hosts=[host],
    )

    result = GcResult()
    gc_machines(
        mngr_ctx=temp_mngr_ctx,
        hosts_by_provider=[(error_provider, _hosts_for(error_provider)[0][1])],
        dry_run=False,
        error_behavior=ErrorBehavior.CONTINUE,
        result=result,
    )

    assert len(result.errors) == 1
    assert "simulated get_host failure from test" in result.errors[0]


def test_gc_machines_handles_mngr_error_with_abort(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """gc_machines re-raises MngrError when ErrorBehavior.ABORT is set."""
    host = _make_offline_host(
        MockProviderInstance(
            name=ProviderInstanceName("dummy"),
            host_dir=temp_host_dir,
            mngr_ctx=temp_mngr_ctx,
        ),
        temp_mngr_ctx,
        days_old=14,
    )

    error_provider = _GetHostErrorProvider(
        name=ProviderInstanceName("error-provider"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
        mock_hosts=[host],
    )

    result = GcResult()
    with pytest.raises(MngrError, match="simulated get_host failure from test"):
        gc_machines(
            mngr_ctx=temp_mngr_ctx,
            hosts_by_provider=[(error_provider, _hosts_for(error_provider)[0][1])],
            dry_run=False,
            error_behavior=ErrorBehavior.ABORT,
            result=result,
        )


# =========================================================================
# gc_snapshots: inner MngrError path
# =========================================================================


class _ListSnapshotsErrorProvider(MockProviderInstance):
    """Provider that raises MngrError from list_snapshots."""

    def list_snapshots(self, host: HostInterface | HostId) -> list[SnapshotInfo]:
        raise MngrError("simulated list_snapshots failure from test")


@pytest.mark.allow_warnings(match=r"^simulated\ list_snapshots\ failure\ from\ test")
def test_gc_snapshots_handles_inner_mngr_error_with_continue(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """gc_snapshots records inner MngrError per-host and continues when CONTINUE behavior."""
    host = _make_offline_host(
        MockProviderInstance(
            name=ProviderInstanceName("dummy"),
            host_dir=temp_host_dir,
            mngr_ctx=temp_mngr_ctx,
        ),
        temp_mngr_ctx,
        days_old=1,
        stop_reason=HostState.DESTROYED.value,
    )

    error_provider = _ListSnapshotsErrorProvider(
        name=ProviderInstanceName("snapshot-error"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
        mock_supports_snapshots=True,
        mock_hosts=[host],
    )

    result = GcResult()
    gc_snapshots(
        hosts_by_provider=[(error_provider, _hosts_for(error_provider)[0][1])],
        dry_run=False,
        error_behavior=ErrorBehavior.CONTINUE,
        result=result,
    )

    assert len(result.errors) == 1
    assert "simulated list_snapshots failure from test" in result.errors[0]
    assert len(result.snapshots_destroyed) == 0


def test_gc_snapshots_handles_inner_mngr_error_with_abort(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """gc_snapshots re-raises inner MngrError when ErrorBehavior.ABORT is set."""
    host = _make_offline_host(
        MockProviderInstance(
            name=ProviderInstanceName("dummy"),
            host_dir=temp_host_dir,
            mngr_ctx=temp_mngr_ctx,
        ),
        temp_mngr_ctx,
        days_old=1,
        stop_reason=HostState.DESTROYED.value,
    )

    error_provider = _ListSnapshotsErrorProvider(
        name=ProviderInstanceName("snapshot-error-abort"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
        mock_supports_snapshots=True,
        mock_hosts=[host],
    )

    result = GcResult()
    with pytest.raises(MngrError, match="simulated list_snapshots failure from test"):
        gc_snapshots(
            hosts_by_provider=[(error_provider, _hosts_for(error_provider)[0][1])],
            dry_run=False,
            error_behavior=ErrorBehavior.ABORT,
            result=result,
        )


# =========================================================================
# gc_volumes: additional coverage tests
# =========================================================================


def test_gc_volumes_skips_destroyed_host_volumes(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """gc_volumes treats volumes of DESTROYED hosts as orphaned.

    A DESTROYED host's volumes have no active owner and should be cleaned up.
    The DESTROYED host is skipped in the active-volume-id loop (line 430).
    """
    destroyed_host_id = HostId("host-00000000000000000000000000000002")
    active_host_id = HostId("host-00000000000000000000000000000003")

    # Volume attached to the destroyed host.
    destroyed_vol = VolumeInfo(
        volume_id=VolumeId("vol-00000000000000000000000000000002"),
        name="destroyed-vol",
        size_bytes=0,
        host_id=destroyed_host_id,
    )
    # Volume attached to the active host.
    active_vol = VolumeInfo(
        volume_id=VolumeId("vol-00000000000000000000000000000003"),
        name="active-vol",
        size_bytes=0,
        host_id=active_host_id,
    )

    provider = MockProviderInstance(
        name=ProviderInstanceName("vol-provider"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
        mock_supports_volumes=True,
        mock_volumes=[destroyed_vol, active_vol],
    )

    active_host_ref = DiscoveredHost(
        host_id=active_host_id,
        host_name=HostName("active-host"),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )
    destroyed_host_ref = DiscoveredHost(
        host_id=destroyed_host_id,
        host_name=HostName("destroyed-host"),
        provider_name=provider.name,
        host_state=HostState.DESTROYED,
    )

    result = GcResult()
    gc_volumes(
        hosts_by_provider=[(provider, [active_host_ref, destroyed_host_ref])],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    # Only the destroyed host's volume should be removed.
    assert len(result.volumes_destroyed) == 1
    assert result.volumes_destroyed[0].volume_id == destroyed_vol.volume_id
    assert provider.deleted_volumes == [destroyed_vol.volume_id]


def test_gc_volumes_preserves_active_host_volumes(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """gc_volumes preserves volumes that belong to active (non-destroyed) hosts."""
    active_host_id = HostId("host-00000000000000000000000000000010")

    vol = VolumeInfo(
        volume_id=VolumeId("vol-00000000000000000000000000000010"),
        name="active-vol",
        size_bytes=0,
        host_id=active_host_id,
    )

    provider = MockProviderInstance(
        name=ProviderInstanceName("vol-provider2"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
        mock_supports_volumes=True,
        mock_volumes=[vol],
    )

    active_host_ref = DiscoveredHost(
        host_id=active_host_id,
        host_name=HostName("active-host"),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )

    result = GcResult()
    gc_volumes(
        hosts_by_provider=[(provider, [active_host_ref])],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.volumes_destroyed) == 0
    assert provider.deleted_volumes == []


class _DeleteVolumeErrorProvider(MockProviderInstance):
    """Provider whose delete_volume raises MngrError."""

    def delete_volume(self, volume_id: VolumeId) -> None:
        raise MngrError(f"simulated delete_volume failure from test: {volume_id}")


@pytest.mark.allow_warnings(match=r"^simulated\ delete_volume\ failure\ from\ test")
def test_gc_volumes_handles_delete_error_with_continue(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """gc_volumes records MngrError from delete_volume and continues."""
    vol = VolumeInfo(
        volume_id=VolumeId("vol-00000000000000000000000000000020"),
        name="broken-vol",
        size_bytes=0,
        host_id=HostId("host-00000000000000000000000000000020"),
    )

    provider = _DeleteVolumeErrorProvider(
        name=ProviderInstanceName("delete-error-provider"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
        mock_supports_volumes=True,
        mock_volumes=[vol],
    )

    result = GcResult()
    gc_volumes(
        hosts_by_provider=[(provider, [])],
        dry_run=False,
        error_behavior=ErrorBehavior.CONTINUE,
        result=result,
    )

    assert len(result.errors) == 1
    assert "broken-vol" in result.errors[0]


def test_gc_volumes_handles_delete_error_with_abort(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """gc_volumes re-raises MngrError from delete_volume when ABORT behavior is set."""
    vol = VolumeInfo(
        volume_id=VolumeId("vol-00000000000000000000000000000021"),
        name="broken-vol-abort",
        size_bytes=0,
        host_id=HostId("host-00000000000000000000000000000021"),
    )

    provider = _DeleteVolumeErrorProvider(
        name=ProviderInstanceName("delete-error-abort"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
        mock_supports_volumes=True,
        mock_volumes=[vol],
    )

    result = GcResult()
    with pytest.raises(MngrError):
        gc_volumes(
            hosts_by_provider=[(provider, [])],
            dry_run=False,
            error_behavior=ErrorBehavior.ABORT,
            result=result,
        )


class _ListVolumesUnavailableProvider(MockProviderInstance):
    """Provider whose list_volumes raises ProviderUnavailableError."""

    def list_volumes(self) -> list[VolumeInfo]:
        raise ProviderUnavailableError(self.name, "backend offline")


class _ListVolumesMngrErrorProvider(MockProviderInstance):
    """Provider whose list_volumes raises a generic MngrError."""

    def list_volumes(self) -> list[VolumeInfo]:
        raise MngrError("simulated list_volumes failure from test")


def test_gc_volumes_skips_provider_when_unavailable(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """gc_volumes skips the provider silently when it raises ProviderUnavailableError."""
    provider = _ListVolumesUnavailableProvider(
        name=ProviderInstanceName("unavailable-provider"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
        mock_supports_volumes=True,
    )

    result = GcResult()
    gc_volumes(
        hosts_by_provider=[(provider, [])],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.volumes_destroyed) == 0
    assert len(result.errors) == 0


@pytest.mark.allow_warnings(match=r"^simulated\ list_volumes\ failure\ from\ test")
def test_gc_volumes_handles_list_volumes_mngr_error_with_continue(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    """gc_volumes records MngrError from list_volumes and continues."""
    provider = _ListVolumesMngrErrorProvider(
        name=ProviderInstanceName("list-error-provider"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
        mock_supports_volumes=True,
    )

    result = GcResult()
    gc_volumes(
        hosts_by_provider=[(provider, [])],
        dry_run=False,
        error_behavior=ErrorBehavior.CONTINUE,
        result=result,
    )

    assert len(result.errors) == 1
    assert "simulated list_volumes failure from test" in result.errors[0]


def test_gc_volumes_handles_list_volumes_mngr_error_with_abort(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    """gc_volumes re-raises MngrError from list_volumes when ABORT behavior is set."""
    provider = _ListVolumesMngrErrorProvider(
        name=ProviderInstanceName("list-error-abort"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
        mock_supports_volumes=True,
    )

    result = GcResult()
    with pytest.raises(MngrError, match="simulated list_volumes failure from test"):
        gc_volumes(
            hosts_by_provider=[(provider, [])],
            dry_run=False,
            error_behavior=ErrorBehavior.ABORT,
            result=result,
        )


# =========================================================================
# gc_logs: absolute log_dir path
# =========================================================================


def test_gc_logs_with_absolute_log_dir(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
    mngr_test_prefix: str,
    active_concurrency_group: ConcurrencyGroup,
) -> None:
    """gc_logs handles an absolute log_dir path (not relative to default_host_dir)."""
    abs_log_dir = tmp_path / "absolute_logs"
    logs_dir = abs_log_dir / "logs" / "mngr"
    logs_dir.mkdir(parents=True, exist_ok=True)

    rotated = logs_dir / "events.jsonl.1"
    rotated.write_text("old content")
    _make_old(rotated, _LOG_MAX_AGE_DAYS + 1)

    abs_config = MngrConfig(
        default_host_dir=temp_host_dir,
        prefix=mngr_test_prefix,
        is_error_reporting_enabled=False,
        logging=LoggingConfig(log_dir=abs_log_dir),
    )
    ctx = make_mngr_ctx(
        abs_config,
        plugin_manager,
        temp_mngr_ctx.profile_dir,
        concurrency_group=active_concurrency_group,
    )

    result = GcResult()
    gc_logs(
        mngr_ctx=ctx,
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.logs_destroyed) == 1
    assert not rotated.exists()


# =========================================================================
# gc_build_cache: non-directory entry skip
# =========================================================================


def test_gc_build_cache_skips_non_directory_provider_entries(
    temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance
) -> None:
    """gc_build_cache skips non-directory entries inside the providers directory."""
    providers_dir = temp_mngr_ctx.profile_dir / "providers"
    providers_dir.mkdir(parents=True, exist_ok=True)

    # Create a plain file directly inside providers/ (not a directory).
    plain_file = providers_dir / "metadata.json"
    plain_file.write_text("{}")

    result = GcResult()
    gc_build_cache(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    # The plain file should not be touched.
    assert plain_file.exists()
    assert len(result.build_cache_destroyed) == 0


# =========================================================================
# Additional coverage: on_resource_type_start callback
# =========================================================================


def test_gc_calls_on_resource_type_start_for_each_enabled_resource_type(
    temp_mngr_ctx: MngrContext, local_provider: LocalProviderInstance
) -> None:
    """gc() calls on_resource_type_start before processing each resource type."""
    calls: list[str] = []

    resource_types = GcResourceTypes(
        is_work_dirs=True,
        is_machines=True,
        is_snapshots=True,
        is_volumes=True,
        is_logs=True,
        is_build_cache=True,
    )
    gc(
        mngr_ctx=temp_mngr_ctx,
        providers=[local_provider],
        resource_types=resource_types,
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        on_resource_type_start=calls.append,
    )

    assert "work_dirs" in calls
    assert "machines" in calls
    assert "snapshots" in calls
    assert "volumes" in calls
    assert "logs" in calls
    assert "build_cache" in calls
    assert len(calls) == 6


# =========================================================================
# Additional coverage: offline host in gc_work_dirs
# =========================================================================


def test_gc_work_dirs_skips_offline_host_not_online_interface(
    gc_mock_provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """gc_work_dirs skips non-online hosts (OfflineHost instances) silently."""
    host = _make_offline_host(gc_mock_provider, temp_mngr_ctx, stop_reason=HostState.STOPPED.value)
    gc_mock_provider.mock_hosts = [host]

    result = GcResult()
    gc_work_dirs(
        mngr_ctx=temp_mngr_ctx,
        hosts_by_provider=_hosts_for(gc_mock_provider),
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.work_dirs_destroyed) == 0
    assert len(result.errors) == 0


# =========================================================================
# Additional coverage: _remove_git_worktree when .git file not found
# =========================================================================


@pytest.mark.allow_warnings(
    match=r"^git\ worktree\ remove\ failed,\ falling\ back\ to\ directory\ removal:\ fatal:\ not\ a\ git\ repository\ \(or\ any\ of\ the\ parent\ directories\):\ \.git"
)
def test_remove_git_worktree_falls_back_when_git_file_absent(local_host: Host, tmp_path: Path) -> None:
    """_remove_git_worktree falls back to rm -rf when the .git file does not exist."""
    work_dir = tmp_path / "worktree_no_git_file"
    work_dir.mkdir()
    # No .git file created - host.read_text_file will raise FileNotFoundError.
    (work_dir / "some_file.py").write_text("code")

    _remove_git_worktree(local_host, work_dir)

    # The directory should have been removed via rm -rf fallback.
    assert not work_dir.exists()


# =========================================================================
# Source dir GC tests (mngr-managed clones from --source <git-url>)
# =========================================================================


def _make_clone_with_remote(
    source_upstream: Path, clone_path: Path, extra_local_branches: tuple[str, ...] = ()
) -> None:
    subprocess.run(
        ["git", "clone", str(source_upstream), str(clone_path)],
        check=True,
        capture_output=True,
    )
    for branch_name in extra_local_branches:
        subprocess.run(
            ["git", "-C", str(clone_path), "checkout", "-b", branch_name],
            check=True,
            capture_output=True,
        )
        (clone_path / f"{branch_name.replace('/', '_')}.marker").write_text(branch_name)
        subprocess.run(["git", "-C", str(clone_path), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(clone_path), "commit", "-m", f"local: {branch_name}"],
            check=True,
            capture_output=True,
        )


def test_local_branches_not_on_any_remote_empty_for_fresh_clone(
    local_host: Host, temp_git_repo: Path, tmp_path: Path
) -> None:
    """A fresh clone has no local branches missing from remotes."""
    clone = tmp_path / "clone"
    _make_clone_with_remote(temp_git_repo, clone)
    assert _local_branches_not_on_any_remote_on_host(local_host, clone) == []


def test_local_branches_not_on_any_remote_finds_local_only_branch(
    local_host: Host, temp_git_repo: Path, tmp_path: Path
) -> None:
    """A local branch with a commit not present on any remote is flagged."""
    clone = tmp_path / "clone"
    _make_clone_with_remote(temp_git_repo, clone, extra_local_branches=("local-only",))
    unpushed = _local_branches_not_on_any_remote_on_host(local_host, clone)
    assert "local-only" in unpushed


def test_get_orphaned_source_dirs_deletes_clean_clone(
    local_host: Host, local_provider: LocalProviderInstance, temp_git_repo: Path, tmp_path: Path
) -> None:
    """A tracked clone with no unpushed branches and no referencing worktree is deletable."""
    clone = tmp_path / "clone"
    _make_clone_with_remote(temp_git_repo, clone)
    register_generated_source_dir(local_host, clone)

    deletable, kept = _get_orphaned_source_dirs(host=local_host, provider_name=local_provider.name)
    assert [info.path for info in deletable] == [clone]
    assert kept == []


def test_get_orphaned_source_dirs_keeps_clone_with_unpushed_branch(
    local_host: Host, local_provider: LocalProviderInstance, temp_git_repo: Path, tmp_path: Path
) -> None:
    """A tracked clone with a local branch not on any remote is kept, not deleted."""
    clone = tmp_path / "clone"
    _make_clone_with_remote(temp_git_repo, clone, extra_local_branches=("mngr/x",))
    register_generated_source_dir(local_host, clone)

    deletable, kept = _get_orphaned_source_dirs(host=local_host, provider_name=local_provider.name)
    assert deletable == []
    assert [info.path for info in kept] == [clone]


def test_local_branches_not_on_any_remote_treats_failure_as_unpushed(local_host: Host, tmp_path: Path) -> None:
    """If git for-each-ref fails (e.g. path is not a git repo), report non-empty so
    the caller keeps the repo rather than deleting it. This guards against data loss
    when branch enumeration cannot succeed.
    """
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    result = _local_branches_not_on_any_remote_on_host(local_host, not_a_repo)
    assert result, "failure must be treated as possibly-unpushed so the caller keeps the repo"


# =========================================================================
# gc_machines: online host minimum age and auth error tests
# =========================================================================


class _RemoteHost(Host):
    """Host subclass that appears remote (is_local=False) with configurable certified data."""

    mock_certified_data: CertifiedHostData | None = None
    mock_last_activity_time: datetime | None = None
    mock_state: HostState | None = None

    @property
    def is_local(self) -> bool:
        return False

    def discover_agents(self) -> list[DiscoveredAgent]:
        return []

    def get_certified_data(self) -> CertifiedHostData:
        if self.mock_certified_data is not None:
            return self.mock_certified_data
        return super().get_certified_data()

    def get_state(self) -> HostState:
        if self.mock_state is not None:
            return self.mock_state
        return super().get_state()

    def get_reported_activity_time(self, activity_type: ActivitySource) -> datetime | None:
        # Report mock_last_activity_time for BOOT so gc sees it as the most recent activity.
        if activity_type == ActivitySource.BOOT and self.mock_last_activity_time is not None:
            return self.mock_last_activity_time
        return None


class _RemoteAuthErrorOnDiscoverHost(_RemoteHost):
    """Remote host where discover_agents raises HostAuthenticationError."""

    def discover_agents(self) -> list[DiscoveredAgent]:
        raise HostAuthenticationError("simulated auth failure from test")


class _RemoteConnectionErrorOnDiscoverHost(_RemoteHost):
    """Remote host where discover_agents raises HostConnectionError."""

    def discover_agents(self) -> list[DiscoveredAgent]:
        raise HostConnectionError("simulated connection failure from test")


class _ActivityTimeAuthErrorHost(_RemoteHost):
    """Remote host where get_reported_activity_time raises HostAuthenticationError."""

    def get_reported_activity_time(self, activity_type: ActivitySource) -> datetime | None:
        raise HostAuthenticationError("simulated auth error reading activity time from test")


class _GetStateAuthErrorHost(_RemoteHost):
    """Remote host where get_state raises HostAuthenticationError after the first call.

    The mock provider's ``discover_hosts`` calls ``get_state`` once to build the
    DiscoveredHost record, so we let that first call through (returning RUNNING)
    and raise on the internal call made by ``_gc_single_host``.
    """

    _get_state_call_count: int = 0

    def get_state(self) -> HostState:
        self._get_state_call_count += 1
        if self._get_state_call_count == 1:
            return HostState.RUNNING
        raise HostAuthenticationError("simulated auth error reading state from test")


class _DestroyableProvider(MockProviderInstance):
    """MockProviderInstance that supports destroy_host."""

    destroyed_hosts: list[HostId] = Field(default_factory=list)

    def destroy_host(self, host: HostInterface | HostId) -> None:
        host_id = host.id if isinstance(host, HostInterface) else host
        self.destroyed_hosts.append(host_id)


def _make_remote_host(
    provider: LocalProviderInstance,
    *,
    last_activity_seconds_ago: float | None = 0,
    created_seconds_ago: float = 0,
    mock_state: HostState | None = None,
    host_cls: type[_RemoteHost] = _RemoteHost,
) -> _RemoteHost:
    """Create a _RemoteHost (or subclass) with configurable time since last activity.

    Pass ``last_activity_seconds_ago=None`` to simulate a host with no
    recorded activity at all (every ActivitySource returns None).
    """
    pyinfra_host = provider._create_local_pyinfra_host()
    connector = PyinfraConnector(pyinfra_host)
    host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    last_activity_time = (
        None if last_activity_seconds_ago is None else now - timedelta(seconds=last_activity_seconds_ago)
    )
    created_at = now - timedelta(seconds=created_seconds_ago)
    certified_data = CertifiedHostData(
        host_id=str(host_id),
        host_name="remote-test-host",
        created_at=created_at,
        updated_at=now,
    )
    return host_cls(
        id=host_id,
        connector=connector,
        provider_instance=provider,
        mngr_ctx=provider.mngr_ctx,
        mock_certified_data=certified_data,
        mock_last_activity_time=last_activity_time,
        mock_state=mock_state,
    )


def _run_gc_on_remote_host(
    host: _RemoteHost,
    *,
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
    provider_name: str,
    dry_run: bool = False,
) -> tuple[_DestroyableProvider, GcResult]:
    """Wrap `host` in a _DestroyableProvider and run gc_machines against it.

    Returns the provider and result so callers can assert on `destroyed_hosts`
    and `machines_destroyed`.
    """
    provider = _DestroyableProvider(
        name=ProviderInstanceName(provider_name),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
        mock_hosts=[host],
    )
    result = GcResult()
    gc_machines(
        mngr_ctx=temp_mngr_ctx,
        hosts_by_provider=_hosts_for(provider),
        dry_run=dry_run,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )
    return provider, result


def test_gc_machines_skips_young_online_host_with_no_agents(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_host_dir: Path,
) -> None:
    """gc_machines skips online hosts with no agents that are younger than the minimum age."""
    host = _make_remote_host(local_provider, last_activity_seconds_ago=60)
    provider, result = _run_gc_on_remote_host(
        host, temp_host_dir=temp_host_dir, temp_mngr_ctx=temp_mngr_ctx, provider_name="test-remote"
    )

    assert len(result.machines_destroyed) == 0
    assert provider.destroyed_hosts == []


def test_gc_machines_destroys_old_online_host_with_no_agents(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_host_dir: Path,
) -> None:
    """gc_machines destroys online hosts with no agents that exceed the minimum age."""
    host = _make_remote_host(local_provider, last_activity_seconds_ago=_DEFAULT_MIN_ONLINE_HOST_AGE_SECONDS + 60)
    provider, result = _run_gc_on_remote_host(
        host, temp_host_dir=temp_host_dir, temp_mngr_ctx=temp_mngr_ctx, provider_name="test-remote-old"
    )

    assert len(result.machines_destroyed) == 1
    assert provider.destroyed_hosts == [host.id]


@pytest.mark.allow_warnings(match=r"^Failed\ to\ authenticate\ with\ host")
def test_gc_machines_skips_host_on_auth_error_during_discover(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_host_dir: Path,
) -> None:
    """gc_machines skips hosts that raise HostAuthenticationError during discover_agents.

    Auth errors are treated conservatively: if we cannot authenticate, we
    cannot verify whether the host has running agents, so it must not be
    destroyed.
    """
    host = _make_remote_host(
        local_provider,
        last_activity_seconds_ago=_DEFAULT_MIN_ONLINE_HOST_AGE_SECONDS + 60,
        host_cls=_RemoteAuthErrorOnDiscoverHost,
    )
    provider, result = _run_gc_on_remote_host(
        host, temp_host_dir=temp_host_dir, temp_mngr_ctx=temp_mngr_ctx, provider_name="test-auth-skip"
    )

    assert len(result.machines_destroyed) == 0
    assert provider.destroyed_hosts == []


@pytest.mark.allow_warnings(match=r"^Failed\ to\ connect\ to\ host")
def test_gc_machines_skips_host_on_connection_error_during_discover(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_host_dir: Path,
) -> None:
    """gc_machines skips hosts that raise HostConnectionError during discover_agents."""
    host = _make_remote_host(
        local_provider,
        last_activity_seconds_ago=_DEFAULT_MIN_ONLINE_HOST_AGE_SECONDS + 60,
        host_cls=_RemoteConnectionErrorOnDiscoverHost,
    )
    provider, result = _run_gc_on_remote_host(
        host, temp_host_dir=temp_host_dir, temp_mngr_ctx=temp_mngr_ctx, provider_name="test-conn-skip"
    )

    assert len(result.machines_destroyed) == 0
    assert provider.destroyed_hosts == []


def test_gc_machines_dry_run_identifies_but_does_not_destroy_old_online_host(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_host_dir: Path,
) -> None:
    """gc_machines dry run reports old online hosts but does not actually destroy them."""
    host = _make_remote_host(local_provider, last_activity_seconds_ago=_DEFAULT_MIN_ONLINE_HOST_AGE_SECONDS + 60)
    provider, result = _run_gc_on_remote_host(
        host,
        temp_host_dir=temp_host_dir,
        temp_mngr_ctx=temp_mngr_ctx,
        provider_name="test-dry-run",
        dry_run=True,
    )

    assert len(result.machines_destroyed) == 1
    assert provider.destroyed_hosts == []


@pytest.mark.allow_warnings(match=r"^Cannot\ determine\ last\ activity\ of\ host")
def test_gc_machines_skips_host_when_activity_time_unreadable(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_host_dir: Path,
) -> None:
    """gc_machines skips hosts where get_reported_activity_time fails (cannot determine activity)."""
    host = _make_remote_host(
        local_provider,
        # mock activity time is unused: _ActivityTimeAuthErrorHost raises unconditionally
        last_activity_seconds_ago=None,
        host_cls=_ActivityTimeAuthErrorHost,
    )
    provider, result = _run_gc_on_remote_host(
        host, temp_host_dir=temp_host_dir, temp_mngr_ctx=temp_mngr_ctx, provider_name="test-cert-error"
    )

    assert len(result.machines_destroyed) == 0
    assert provider.destroyed_hosts == []


def test_gc_machines_skips_young_host_with_no_activity(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_host_dir: Path,
) -> None:
    """Young host with no activity is in setup grace period -- skip."""
    host = _make_remote_host(
        local_provider,
        last_activity_seconds_ago=None,
        created_seconds_ago=60,
    )
    provider, result = _run_gc_on_remote_host(
        host, temp_host_dir=temp_host_dir, temp_mngr_ctx=temp_mngr_ctx, provider_name="test-young-no-activity"
    )

    assert len(result.machines_destroyed) == 0
    assert provider.destroyed_hosts == []


def test_gc_machines_destroys_old_crashed_host_with_no_activity(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_host_dir: Path,
) -> None:
    """Host past grace period with no activity and CRASHED state -- destroy.

    This is the case Josh flagged: a host that crashed so hard nothing wrote
    any activity file.  Without this path, such hosts would never be cleaned up.
    """
    host = _make_remote_host(
        local_provider,
        last_activity_seconds_ago=None,
        created_seconds_ago=_DEFAULT_MIN_ONLINE_HOST_AGE_SECONDS + 60,
        mock_state=HostState.CRASHED,
    )
    provider, result = _run_gc_on_remote_host(
        host, temp_host_dir=temp_host_dir, temp_mngr_ctx=temp_mngr_ctx, provider_name="test-crashed-no-activity"
    )

    assert len(result.machines_destroyed) == 1
    assert provider.destroyed_hosts == [host.id]


def test_gc_machines_skips_old_running_host_with_no_activity(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_host_dir: Path,
) -> None:
    """Host past grace period with no activity but RUNNING state -- skip.

    Healthy hosts that happen to have no mngr agents are the user's call, not GC's.
    """
    host = _make_remote_host(
        local_provider,
        last_activity_seconds_ago=None,
        created_seconds_ago=_DEFAULT_MIN_ONLINE_HOST_AGE_SECONDS + 60,
        mock_state=HostState.RUNNING,
    )
    provider, result = _run_gc_on_remote_host(
        host, temp_host_dir=temp_host_dir, temp_mngr_ctx=temp_mngr_ctx, provider_name="test-running-no-activity"
    )

    assert len(result.machines_destroyed) == 0
    assert provider.destroyed_hosts == []


@pytest.mark.allow_warnings(match=r"^Cannot\ determine\ state\ of\ host")
def test_gc_machines_skips_host_when_get_state_unreadable(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_host_dir: Path,
) -> None:
    """gc_machines skips hosts where get_state fails (cannot determine terminal state).

    Reaches the no-activity branch past the grace period, then fails to read
    state.  Must not destroy: without a terminal state we cannot distinguish a
    crashed host from a healthy one.
    """
    host = _make_remote_host(
        local_provider,
        last_activity_seconds_ago=None,
        created_seconds_ago=_DEFAULT_MIN_ONLINE_HOST_AGE_SECONDS + 60,
        host_cls=_GetStateAuthErrorHost,
    )
    provider, result = _run_gc_on_remote_host(
        host, temp_host_dir=temp_host_dir, temp_mngr_ctx=temp_mngr_ctx, provider_name="test-state-auth-error"
    )

    assert len(result.machines_destroyed) == 0
    assert provider.destroyed_hosts == []
