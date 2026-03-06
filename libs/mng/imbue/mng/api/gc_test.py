"""Unit tests for gc API functions."""

import os
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mng.api.data_types import GcResourceTypes
from imbue.mng.api.data_types import GcResult
from imbue.mng.api.gc import _LOG_MAX_AGE_DAYS
from imbue.mng.api.gc import _handle_error
from imbue.mng.api.gc import _is_rotated_log_file
from imbue.mng.api.gc import gc
from imbue.mng.api.gc import gc_build_cache
from imbue.mng.api.gc import gc_logs
from imbue.mng.api.gc import gc_machines
from imbue.mng.api.gc import gc_snapshots
from imbue.mng.api.gc import gc_volumes
from imbue.mng.api.gc import gc_work_dirs
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import MngError
from imbue.mng.hosts.offline_host import OfflineHost
from imbue.mng.interfaces.data_types import CertifiedHostData
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostState
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.local.instance import LocalProviderInstance
from imbue.mng.providers.mock_provider_test import MockProviderInstance
from imbue.mng.providers.mock_provider_test import make_offline_host


def test_gc_machines_skips_local_hosts(local_provider: LocalProviderInstance, temp_mng_ctx: MngContext) -> None:
    """Test that gc_machines skips local hosts even when they have no agents."""
    result = GcResult()

    gc_machines(
        mng_ctx=temp_mng_ctx,
        providers=[local_provider],
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


@pytest.fixture
def gc_mock_provider(temp_host_dir: Path, temp_mng_ctx: MngContext) -> MockProviderInstance:
    """Create a MockProviderInstance for gc_machines tests."""
    return MockProviderInstance(
        name=ProviderInstanceName("test-provider"),
        host_dir=temp_host_dir,
        mng_ctx=temp_mng_ctx,
    )


def _make_offline_host(
    provider: MockProviderInstance,
    mng_ctx: MngContext,
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
    return make_offline_host(certified_data, provider, mng_ctx)


def _run_gc_machines(provider: MockProviderInstance, *, dry_run: bool = False) -> GcResult:
    """Run gc_machines on a single provider and return the result."""
    result = GcResult()
    gc_machines(
        mng_ctx=provider.mng_ctx,
        providers=[provider],
        dry_run=dry_run,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )
    return result


def test_gc_machines_deletes_old_offline_host_with_no_agents(
    gc_mock_provider: MockProviderInstance, temp_mng_ctx: MngContext
) -> None:
    """Old offline hosts with no agents are deleted to prevent data accumulation."""
    host = _make_offline_host(gc_mock_provider, temp_mng_ctx, days_old=14)
    gc_mock_provider.mock_hosts = [host]

    result = _run_gc_machines(gc_mock_provider)

    assert len(result.machines_deleted) == 1
    assert result.machines_deleted[0].host_id == host.id
    assert gc_mock_provider.deleted_hosts == [host.id]


def test_gc_machines_skips_recent_offline_host(
    gc_mock_provider: MockProviderInstance, temp_mng_ctx: MngContext
) -> None:
    """Offline hosts stopped less than the max persisted seconds ago are not deleted."""
    host = _make_offline_host(gc_mock_provider, temp_mng_ctx, days_old=1)
    gc_mock_provider.mock_hosts = [host]

    result = _run_gc_machines(gc_mock_provider)

    assert len(result.machines_deleted) == 0
    assert gc_mock_provider.deleted_hosts == []


def _add_mock_agent(provider: MockProviderInstance) -> None:
    """Add a mock agent to the provider so hosts appear to have agents."""
    agent_id = AgentId.generate()
    provider.mock_agent_data = [{"id": str(agent_id), "name": "test-agent"}]


def test_gc_machines_deletes_old_crashed_host_with_agents(
    gc_mock_provider: MockProviderInstance, temp_mng_ctx: MngContext
) -> None:
    """Old offline hosts in CRASHED state are deleted even if they have agents."""
    # None stop_reason means the host CRASHED
    host = _make_offline_host(gc_mock_provider, temp_mng_ctx, stop_reason=None)
    _add_mock_agent(gc_mock_provider)
    gc_mock_provider.mock_hosts = [host]

    result = _run_gc_machines(gc_mock_provider)

    assert len(result.machines_deleted) == 1
    assert gc_mock_provider.deleted_hosts == [host.id]


def test_gc_machines_skips_old_stopped_host_with_agents(
    gc_mock_provider: MockProviderInstance, temp_mng_ctx: MngContext
) -> None:
    """Old offline hosts in STOPPED state with agents are not deleted."""
    host = _make_offline_host(gc_mock_provider, temp_mng_ctx, days_old=14)
    _add_mock_agent(gc_mock_provider)
    gc_mock_provider.mock_hosts = [host]

    result = _run_gc_machines(gc_mock_provider)

    assert len(result.machines_deleted) == 0
    assert gc_mock_provider.deleted_hosts == []


def test_gc_machines_dry_run_does_not_call_delete_host(
    gc_mock_provider: MockProviderInstance, temp_mng_ctx: MngContext
) -> None:
    """Dry run identifies hosts for deletion but does not actually delete them."""
    host = _make_offline_host(gc_mock_provider, temp_mng_ctx, days_old=14)
    gc_mock_provider.mock_hosts = [host]

    result = _run_gc_machines(gc_mock_provider, dry_run=True)

    assert len(result.machines_deleted) == 1
    assert gc_mock_provider.deleted_hosts == []


def test_gc_machines_deletes_old_failed_host_with_agents(
    gc_mock_provider: MockProviderInstance, temp_mng_ctx: MngContext
) -> None:
    """Old offline hosts in FAILED state are deleted even if they have agents."""
    host = _make_offline_host(gc_mock_provider, temp_mng_ctx, failure_reason="Build failed")
    _add_mock_agent(gc_mock_provider)
    gc_mock_provider.mock_hosts = [host]

    result = _run_gc_machines(gc_mock_provider)

    assert len(result.machines_deleted) == 1
    assert gc_mock_provider.deleted_hosts == [host.id]


def test_gc_machines_deletes_old_destroyed_host_with_agents(
    gc_mock_provider: MockProviderInstance, temp_mng_ctx: MngContext
) -> None:
    """Old offline hosts in DESTROYED state are deleted even if they have agents."""
    host = _make_offline_host(gc_mock_provider, temp_mng_ctx)
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
    exc = MngError("test error")
    with pytest.raises(MngError, match="test error"):
        _handle_error("some message", ErrorBehavior.ABORT, exc=exc)


def test_handle_error_abort_raises_mng_error_when_no_exception() -> None:
    """ABORT behavior raises MngError from the message when no exception is provided."""
    with pytest.raises(MngError, match="some message"):
        _handle_error("some message", ErrorBehavior.ABORT, exc=None)


def test_handle_error_continue_does_not_raise() -> None:
    """CONTINUE behavior logs instead of raising."""
    # Should not raise
    _handle_error("some message", ErrorBehavior.CONTINUE, exc=MngError("test"))
    _handle_error("some message", ErrorBehavior.CONTINUE, exc=None)


# =========================================================================
# gc_logs tests
# =========================================================================


def _make_old(path: Path, days: int) -> None:
    """Set a file's mtime to be `days` old by backdating atime/mtime."""
    old_time = time.time() - (days * 86400)
    os.utime(path, (old_time, old_time))


def test_gc_logs_deletes_old_rotated_files(temp_mng_ctx: MngContext, local_provider: LocalProviderInstance) -> None:
    """gc_logs deletes rotated log files older than 30 days under events/logs/."""
    events_dir = temp_mng_ctx.config.default_host_dir.expanduser() / temp_mng_ctx.config.logging.log_dir
    logs_dir = events_dir / "logs" / "mng"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Create a rotated log file and make it old
    rotated = logs_dir / "events.jsonl.1"
    rotated.write_text("old log content")
    _make_old(rotated, _LOG_MAX_AGE_DAYS + 1)

    result = GcResult()
    gc_logs(
        mng_ctx=temp_mng_ctx,
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.logs_destroyed) == 1
    assert not rotated.exists()


def test_gc_logs_preserves_current_log_file(temp_mng_ctx: MngContext, local_provider: LocalProviderInstance) -> None:
    """gc_logs never deletes the current log file (events.jsonl)."""
    events_dir = temp_mng_ctx.config.default_host_dir.expanduser() / temp_mng_ctx.config.logging.log_dir
    logs_dir = events_dir / "logs" / "mng"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Create the current log file and make it old
    current = logs_dir / "events.jsonl"
    current.write_text("current log content")
    _make_old(current, _LOG_MAX_AGE_DAYS + 10)

    result = GcResult()
    gc_logs(
        mng_ctx=temp_mng_ctx,
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.logs_destroyed) == 0
    assert current.exists()


def test_gc_logs_preserves_recent_rotated_files(
    temp_mng_ctx: MngContext, local_provider: LocalProviderInstance
) -> None:
    """gc_logs preserves rotated files that are younger than 30 days."""
    events_dir = temp_mng_ctx.config.default_host_dir.expanduser() / temp_mng_ctx.config.logging.log_dir
    logs_dir = events_dir / "logs" / "mng"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Create a recent rotated file (should be preserved)
    recent = logs_dir / "events.jsonl.1"
    recent.write_text("recent rotated content")
    # Don't backdate -- it's brand new

    result = GcResult()
    gc_logs(
        mng_ctx=temp_mng_ctx,
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.logs_destroyed) == 0
    assert recent.exists()


def test_gc_logs_dry_run_does_not_delete(temp_mng_ctx: MngContext, local_provider: LocalProviderInstance) -> None:
    """In dry_run mode, gc_logs identifies files but does not delete them."""
    events_dir = temp_mng_ctx.config.default_host_dir.expanduser() / temp_mng_ctx.config.logging.log_dir
    logs_dir = events_dir / "logs" / "mng"
    logs_dir.mkdir(parents=True, exist_ok=True)

    rotated = logs_dir / "events.jsonl.1"
    rotated.write_text("log content")
    _make_old(rotated, _LOG_MAX_AGE_DAYS + 1)

    result = GcResult()
    gc_logs(
        mng_ctx=temp_mng_ctx,
        providers=[local_provider],
        dry_run=True,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.logs_destroyed) == 1
    assert rotated.exists()


def test_gc_logs_skips_nonexistent_directory(temp_mng_ctx: MngContext, local_provider: LocalProviderInstance) -> None:
    """gc_logs returns early when the logs directory does not exist."""
    result = GcResult()
    gc_logs(
        mng_ctx=temp_mng_ctx,
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.logs_destroyed) == 0


def test_gc_logs_does_not_touch_event_files_outside_logs(
    temp_mng_ctx: MngContext, local_provider: LocalProviderInstance
) -> None:
    """gc_logs only targets events/logs/, not other directories under events/."""
    events_dir = temp_mng_ctx.config.default_host_dir.expanduser() / temp_mng_ctx.config.logging.log_dir

    # Create a non-log event file directly under events/
    conversations_dir = events_dir / "conversations"
    conversations_dir.mkdir(parents=True, exist_ok=True)
    event_file = conversations_dir / "events.jsonl.1"
    event_file.write_text("conversation event")
    _make_old(event_file, _LOG_MAX_AGE_DAYS + 10)

    result = GcResult()
    gc_logs(
        mng_ctx=temp_mng_ctx,
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.logs_destroyed) == 0
    assert event_file.exists()


def test_gc_logs_populates_log_file_info(temp_mng_ctx: MngContext, local_provider: LocalProviderInstance) -> None:
    """gc_logs populates LogFileInfo with correct path, size, and creation time."""
    events_dir = temp_mng_ctx.config.default_host_dir.expanduser() / temp_mng_ctx.config.logging.log_dir
    logs_dir = events_dir / "logs" / "mng"
    logs_dir.mkdir(parents=True, exist_ok=True)

    content = "some log content here"
    rotated = logs_dir / "events.jsonl.1"
    rotated.write_text(content)
    _make_old(rotated, _LOG_MAX_AGE_DAYS + 5)

    result = GcResult()
    gc_logs(
        mng_ctx=temp_mng_ctx,
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
    temp_mng_ctx: MngContext, local_provider: LocalProviderInstance
) -> None:
    """gc_build_cache finds cache entry directories and deletes them."""
    cache_dir = temp_mng_ctx.profile_dir / "providers" / "some-provider" / "cache"
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
        mng_ctx=temp_mng_ctx,
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
    temp_mng_ctx: MngContext, local_provider: LocalProviderInstance
) -> None:
    """In dry_run mode, gc_build_cache identifies entries but does not delete them."""
    cache_dir = temp_mng_ctx.profile_dir / "providers" / "some-provider" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    entry = cache_dir / "entry-1"
    entry.mkdir()
    (entry / "data.bin").write_text("binary data")

    result = GcResult()
    gc_build_cache(
        mng_ctx=temp_mng_ctx,
        providers=[local_provider],
        dry_run=True,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.build_cache_destroyed) >= 1
    # Directory should still exist
    assert entry.exists()


def test_gc_build_cache_skips_nonexistent_directory(
    temp_mng_ctx: MngContext, local_provider: LocalProviderInstance
) -> None:
    """gc_build_cache returns early when the providers directory does not exist."""
    result = GcResult()
    gc_build_cache(
        mng_ctx=temp_mng_ctx,
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.build_cache_destroyed) == 0


def test_gc_build_cache_skips_provider_without_cache_dir(
    temp_mng_ctx: MngContext, local_provider: LocalProviderInstance
) -> None:
    """gc_build_cache skips provider directories that have no cache subdirectory."""
    providers_dir = temp_mng_ctx.profile_dir / "providers" / "some-provider"
    providers_dir.mkdir(parents=True, exist_ok=True)
    # No "cache" subdirectory created

    result = GcResult()
    gc_build_cache(
        mng_ctx=temp_mng_ctx,
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.build_cache_destroyed) == 0


def test_gc_build_cache_populates_build_cache_info(
    temp_mng_ctx: MngContext, local_provider: LocalProviderInstance
) -> None:
    """gc_build_cache populates BuildCacheInfo with correct path, size, and creation time."""
    cache_dir = temp_mng_ctx.profile_dir / "providers" / "test-provider" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    entry = cache_dir / "entry-1"
    entry.mkdir()
    content = "some cached data"
    (entry / "file.bin").write_text(content)

    result = GcResult()
    gc_build_cache(
        mng_ctx=temp_mng_ctx,
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


def test_gc_with_only_logs_flag(temp_mng_ctx: MngContext, local_provider: LocalProviderInstance) -> None:
    """gc() only collects logs when is_logs=True and other flags are False."""
    events_dir = temp_mng_ctx.config.default_host_dir.expanduser() / temp_mng_ctx.config.logging.log_dir
    logs_dir = events_dir / "logs" / "mng"
    logs_dir.mkdir(parents=True, exist_ok=True)
    rotated = logs_dir / "events.jsonl.1"
    rotated.write_text("content")
    _make_old(rotated, _LOG_MAX_AGE_DAYS + 1)

    resource_types = GcResourceTypes(is_logs=True)
    result = gc(
        mng_ctx=temp_mng_ctx,
        providers=[local_provider],
        resource_types=resource_types,
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )

    assert len(result.logs_destroyed) == 1
    assert len(result.machines_destroyed) == 0
    assert len(result.build_cache_destroyed) == 0


def test_gc_with_only_build_cache_flag(temp_mng_ctx: MngContext, local_provider: LocalProviderInstance) -> None:
    """gc() only collects build cache when is_build_cache=True and other flags are False."""
    cache_dir = temp_mng_ctx.profile_dir / "providers" / "prov" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    entry = cache_dir / "entry-1"
    entry.mkdir()
    (entry / "data").write_text("data")

    resource_types = GcResourceTypes(is_build_cache=True)
    result = gc(
        mng_ctx=temp_mng_ctx,
        providers=[local_provider],
        resource_types=resource_types,
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )

    assert len(result.build_cache_destroyed) >= 1
    assert len(result.machines_destroyed) == 0
    assert len(result.logs_destroyed) == 0


def test_gc_with_no_flags_does_nothing(temp_mng_ctx: MngContext, local_provider: LocalProviderInstance) -> None:
    """gc() does nothing when all resource type flags are False."""
    resource_types = GcResourceTypes()
    result = gc(
        mng_ctx=temp_mng_ctx,
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


def test_gc_with_multiple_flags(temp_mng_ctx: MngContext, local_provider: LocalProviderInstance) -> None:
    """gc() collects both logs and build cache when both flags are set."""
    # Set up logs
    events_dir = temp_mng_ctx.config.default_host_dir.expanduser() / temp_mng_ctx.config.logging.log_dir
    logs_dir = events_dir / "logs" / "mng"
    logs_dir.mkdir(parents=True, exist_ok=True)
    rotated = logs_dir / "events.jsonl.1"
    rotated.write_text("content")
    _make_old(rotated, _LOG_MAX_AGE_DAYS + 1)

    # Set up build cache
    cache_dir = temp_mng_ctx.profile_dir / "providers" / "prov" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    entry = cache_dir / "entry-1"
    entry.mkdir()
    (entry / "data").write_text("data")

    resource_types = GcResourceTypes(is_logs=True, is_build_cache=True)
    result = gc(
        mng_ctx=temp_mng_ctx,
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
    temp_mng_ctx: MngContext, local_provider: LocalProviderInstance
) -> None:
    """gc_work_dirs should find no orphaned work dirs on a fresh local provider."""
    result = GcResult()
    gc_work_dirs(
        mng_ctx=temp_mng_ctx,
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )
    assert len(result.work_dirs_destroyed) == 0


# =========================================================================
# gc_snapshots tests
# =========================================================================


def test_gc_snapshots_skips_provider_without_snapshot_support(
    temp_mng_ctx: MngContext, local_provider: LocalProviderInstance
) -> None:
    """gc_snapshots should skip providers that do not support snapshots."""
    result = GcResult()
    gc_snapshots(
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )
    assert len(result.snapshots_destroyed) == 0
    assert len(result.errors) == 0


# =========================================================================
# gc_volumes tests
# =========================================================================


def test_gc_volumes_skips_provider_without_volume_support(
    temp_mng_ctx: MngContext, local_provider: LocalProviderInstance
) -> None:
    """gc_volumes should skip providers that do not support volumes."""
    result = GcResult()
    gc_volumes(
        providers=[local_provider],
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )
    assert len(result.volumes_destroyed) == 0
    assert len(result.errors) == 0


# =========================================================================
# gc() with work_dirs, snapshots, volumes flags
# =========================================================================


def test_gc_with_work_dirs_flag(temp_mng_ctx: MngContext, local_provider: LocalProviderInstance) -> None:
    """gc() with is_work_dirs=True should run work directory garbage collection."""
    resource_types = GcResourceTypes(is_work_dirs=True)
    result = gc(
        mng_ctx=temp_mng_ctx,
        providers=[local_provider],
        resource_types=resource_types,
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )
    assert len(result.work_dirs_destroyed) == 0


def test_gc_with_snapshots_flag(temp_mng_ctx: MngContext, local_provider: LocalProviderInstance) -> None:
    """gc() with is_snapshots=True should run snapshot garbage collection."""
    resource_types = GcResourceTypes(is_snapshots=True)
    result = gc(
        mng_ctx=temp_mng_ctx,
        providers=[local_provider],
        resource_types=resource_types,
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )
    assert len(result.snapshots_destroyed) == 0


def test_gc_with_volumes_flag(temp_mng_ctx: MngContext, local_provider: LocalProviderInstance) -> None:
    """gc() with is_volumes=True should run volume garbage collection."""
    resource_types = GcResourceTypes(is_volumes=True)
    result = gc(
        mng_ctx=temp_mng_ctx,
        providers=[local_provider],
        resource_types=resource_types,
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )
    assert len(result.volumes_destroyed) == 0


def test_gc_with_machines_flag(temp_mng_ctx: MngContext, local_provider: LocalProviderInstance) -> None:
    """gc() with is_machines=True should run machine garbage collection."""
    resource_types = GcResourceTypes(is_machines=True)
    result = gc(
        mng_ctx=temp_mng_ctx,
        providers=[local_provider],
        resource_types=resource_types,
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )
    assert len(result.machines_destroyed) == 0
