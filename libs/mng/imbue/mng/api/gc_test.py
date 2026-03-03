"""Unit tests for gc API functions."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mng.api.data_types import GcResult
from imbue.mng.api.gc import gc_machines
from imbue.mng.config.data_types import MngContext
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
    assert result.machines_deleted[0].id == host.id
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
