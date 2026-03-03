"""Unit tests for OfflineHost implementation."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mng.config.data_types import MngContext
from imbue.mng.hosts.offline_host import OfflineHost
from imbue.mng.hosts.offline_host import validate_and_create_agent_reference
from imbue.mng.interfaces.data_types import ActivityConfig
from imbue.mng.interfaces.data_types import CertifiedHostData
from imbue.mng.interfaces.data_types import SnapshotInfo
from imbue.mng.primitives import ActivitySource
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostState
from imbue.mng.primitives import IdleMode
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import SnapshotId
from imbue.mng.primitives import SnapshotName
from imbue.mng.providers.mock_provider_test import MockProviderInstance
from imbue.mng.providers.mock_provider_test import make_offline_host


@pytest.fixture
def fake_provider(temp_host_dir: Path, temp_mng_ctx: MngContext) -> MockProviderInstance:
    """Create a MockProviderInstance with sensible defaults for OfflineHost tests."""
    return MockProviderInstance(
        name=ProviderInstanceName("test-provider"),
        host_dir=temp_host_dir,
        mng_ctx=temp_mng_ctx,
        mock_supports_snapshots=True,
        mock_tags={"env": "test"},
    )


@pytest.fixture
def offline_host(fake_provider: MockProviderInstance, temp_mng_ctx: MngContext) -> OfflineHost:
    """Create an OfflineHost instance for testing."""
    host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    certified_data = CertifiedHostData(
        host_id=str(host_id),
        host_name="test-host",
        idle_timeout_seconds=3600,
        activity_sources=(ActivitySource.SSH, ActivitySource.CREATE, ActivitySource.START, ActivitySource.BOOT),
        image="test-image:latest",
        plugin={"my_plugin": {"key": "value"}},
        created_at=now,
        updated_at=now,
    )
    return OfflineHost(
        id=host_id,
        certified_host_data=certified_data,
        provider_instance=fake_provider,
        mng_ctx=temp_mng_ctx,
    )


def test_get_activity_config_returns_config_from_certified_data(offline_host: OfflineHost) -> None:
    """Test that get_activity_config returns the correct ActivityConfig."""
    config = offline_host.get_activity_config()

    assert isinstance(config, ActivityConfig)
    assert config.idle_mode == IdleMode.SSH
    assert config.idle_timeout_seconds == 3600
    assert config.activity_sources == (
        ActivitySource.SSH,
        ActivitySource.CREATE,
        ActivitySource.START,
        ActivitySource.BOOT,
    )


def test_get_stop_time_returns_updated_at(offline_host: OfflineHost) -> None:
    """Test that get_stop_time returns the updated_at timestamp from certified data."""
    stop_time = offline_host.get_stop_time()

    assert stop_time is not None
    assert stop_time == offline_host.certified_host_data.updated_at


def test_get_seconds_since_stopped_returns_elapsed_time(
    fake_provider: MockProviderInstance, temp_mng_ctx: MngContext
) -> None:
    """Test that get_seconds_since_stopped computes elapsed time from updated_at."""
    host_id = HostId.generate()
    five_minutes_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
    certified_data = CertifiedHostData(
        host_id=str(host_id),
        host_name="stopped-host",
        created_at=five_minutes_ago,
        updated_at=five_minutes_ago,
    )
    host = OfflineHost(
        id=host_id,
        certified_host_data=certified_data,
        provider_instance=fake_provider,
        mng_ctx=temp_mng_ctx,
    )

    seconds = host.get_seconds_since_stopped()
    assert seconds is not None
    # Should be approximately 300 seconds (5 minutes), with some tolerance
    assert 290 < seconds < 310


def test_get_all_certified_data_returns_stored_data(offline_host: OfflineHost) -> None:
    """Test that get_all_certified_data returns the certified host data."""
    data = offline_host.get_certified_data()

    assert isinstance(data, CertifiedHostData)
    assert data.image == "test-image:latest"
    assert data.idle_mode == IdleMode.SSH


def test_get_plugin_data_returns_plugin_data_when_present(offline_host: OfflineHost) -> None:
    """Test that get_plugin_data returns data for existing plugins."""
    data = offline_host.get_plugin_data("my_plugin")
    assert data == {"key": "value"}


def test_get_plugin_data_returns_empty_dict_when_missing(offline_host: OfflineHost) -> None:
    """Test that get_plugin_data returns empty dict for non-existent plugins."""
    data = offline_host.get_plugin_data("nonexistent_plugin")
    assert data == {}


def test_get_snapshots_delegates_to_provider(offline_host: OfflineHost, fake_provider: MockProviderInstance) -> None:
    """Test that get_snapshots returns snapshots from the provider."""
    expected_snapshots = [
        SnapshotInfo(
            id=SnapshotId("snap-test-1"),
            name=SnapshotName("snap1"),
            created_at=datetime.now(timezone.utc),
        )
    ]
    fake_provider.mock_snapshots = expected_snapshots

    snapshots = offline_host.get_snapshots()

    assert snapshots == expected_snapshots


def test_get_image_returns_image_from_certified_data(offline_host: OfflineHost) -> None:
    """Test that get_image returns the image from certified data."""
    image = offline_host.get_image()
    assert image == "test-image:latest"


def test_get_tags_delegates_to_provider(offline_host: OfflineHost) -> None:
    """Test that get_tags returns tags from the provider."""
    tags = offline_host.get_tags()

    assert tags == {"env": "test"}


def test_get_agent_references_returns_refs_from_provider(
    offline_host: OfflineHost, fake_provider: MockProviderInstance
) -> None:
    """Test that get_agent_references loads agent data from provider and populates certified_data."""
    agent_id_1 = AgentId.generate()
    agent_id_2 = AgentId.generate()
    agent_data_1 = {"id": str(agent_id_1), "name": "my-agent", "type": "claude", "permissions": ["read", "write"]}
    agent_data_2 = {"id": str(agent_id_2), "name": "other-agent", "type": "codex"}
    fake_provider.mock_agent_data = [agent_data_1, agent_data_2]

    refs = offline_host.get_agent_references()

    assert len(refs) == 2
    assert refs[0].agent_id == agent_id_1
    assert refs[0].agent_name == AgentName("my-agent")
    assert refs[0].host_id == offline_host.id
    assert refs[0].provider_name == ProviderInstanceName("test-provider")
    # Verify certified_data is populated with full agent data
    assert refs[0].certified_data == agent_data_1
    assert refs[0].agent_type == "claude"
    assert refs[0].permissions == ("read", "write")

    assert refs[1].agent_id == agent_id_2
    assert refs[1].agent_name == AgentName("other-agent")
    assert refs[1].certified_data == agent_data_2
    assert refs[1].agent_type == "codex"
    assert refs[1].permissions == ()


def test_get_agent_references_returns_empty_list_on_error(
    offline_host: OfflineHost, fake_provider: MockProviderInstance
) -> None:
    """Test that get_agent_references returns empty list when agent data is malformed."""
    fake_provider.mock_agent_data = [{"invalid_key": "missing id and name"}]

    refs = offline_host.get_agent_references()
    assert refs == []


def test_get_permissions_returns_empty_list_when_no_agents(offline_host: OfflineHost) -> None:
    """Test that get_permissions returns empty list when no agents exist."""
    permissions = offline_host.get_permissions()
    assert permissions == []


def test_get_permissions_returns_permissions_from_agents(
    offline_host: OfflineHost, fake_provider: MockProviderInstance
) -> None:
    """Test that get_permissions returns union of all agent permissions from persisted data."""
    agent_id_1 = AgentId.generate()
    agent_id_2 = AgentId.generate()
    fake_provider.mock_agent_data = [
        {"id": str(agent_id_1), "name": "agent-1", "permissions": ["read", "write"]},
        {"id": str(agent_id_2), "name": "agent-2", "permissions": ["write", "execute"]},
    ]

    permissions = offline_host.get_permissions()

    # Should be the union of all permissions
    assert set(permissions) == {"read", "write", "execute"}


def test_get_state_returns_crashed_when_no_stop_reason(offline_host: OfflineHost) -> None:
    """Test that get_state returns CRASHED when no stop_reason is set."""
    state = offline_host.get_state()
    # No stop_reason means host didn't shut down cleanly
    assert state == HostState.CRASHED


def test_get_state_returns_crashed_when_provider_does_not_support_snapshots_and_no_stop_reason(
    offline_host: OfflineHost, fake_provider: MockProviderInstance
) -> None:
    """Test that get_state returns CRASHED when provider doesn't support snapshots and no stop_reason."""
    fake_provider.mock_supports_snapshots = False

    state = offline_host.get_state()
    # No stop_reason means host didn't shut down cleanly
    assert state == HostState.CRASHED


def test_get_state_returns_failed_when_certified_data_has_failure_reason(
    fake_provider: MockProviderInstance, temp_mng_ctx: MngContext
) -> None:
    """Test that get_state returns FAILED when certified data has a failure_reason."""
    now = datetime.now(timezone.utc)
    certified_data = CertifiedHostData(
        host_id=str(HostId.generate()),
        host_name="failed-host",
        failure_reason="Docker build failed",
        build_log="Step 1/5: RUN apt-get update\nERROR: apt-get failed",
        created_at=now,
        updated_at=now,
    )
    failed_host = make_offline_host(certified_data, fake_provider, temp_mng_ctx)

    state = failed_host.get_state()
    assert state == HostState.FAILED


def test_get_failure_reason_returns_reason_when_present(
    fake_provider: MockProviderInstance, temp_mng_ctx: MngContext
) -> None:
    """Test that get_failure_reason returns the failure reason from certified data."""
    now = datetime.now(timezone.utc)
    certified_data = CertifiedHostData(
        host_id=str(HostId.generate()),
        host_name="failed-host",
        failure_reason="Modal sandbox creation failed",
        build_log="Build log contents",
        created_at=now,
        updated_at=now,
    )
    failed_host = make_offline_host(certified_data, fake_provider, temp_mng_ctx)

    reason = failed_host.get_failure_reason()
    assert reason == "Modal sandbox creation failed"


def test_get_failure_reason_returns_none_for_successful_host(offline_host: OfflineHost) -> None:
    """Test that get_failure_reason returns None for hosts that did not fail."""
    reason = offline_host.get_failure_reason()
    assert reason is None


def test_get_build_log_returns_log_when_present(fake_provider: MockProviderInstance, temp_mng_ctx: MngContext) -> None:
    """Test that get_build_log returns the build log from certified data."""
    build_log_content = "Step 1/5: FROM ubuntu:22.04\nStep 2/5: RUN apt-get update\nERROR: network error"
    now = datetime.now(timezone.utc)
    certified_data = CertifiedHostData(
        host_id=str(HostId.generate()),
        host_name="failed-host",
        failure_reason="Build failed",
        build_log=build_log_content,
        created_at=now,
        updated_at=now,
    )
    failed_host = make_offline_host(certified_data, fake_provider, temp_mng_ctx)

    log = failed_host.get_build_log()
    assert log == build_log_content


def test_get_build_log_returns_none_for_successful_host(offline_host: OfflineHost) -> None:
    """Test that get_build_log returns None for hosts that did not fail."""
    log = offline_host.get_build_log()
    assert log is None


def test_get_state_returns_crashed_when_no_stop_reason_regardless_of_snapshot_support(
    offline_host: OfflineHost,
) -> None:
    """Test that get_state returns CRASHED when stop_reason is None, regardless of provider capabilities."""
    # The offline_host has no stop_reason set (defaults to None), so get_state
    # returns CRASHED before ever checking snapshot support.
    state = offline_host.get_state()
    assert state == HostState.CRASHED


def test_failure_reason_takes_precedence_over_snapshot_check(
    fake_provider: MockProviderInstance, temp_mng_ctx: MngContext
) -> None:
    """Test that FAILED is returned when failure_reason is set, even when snapshots exist."""
    now = datetime.now(timezone.utc)
    certified_data = CertifiedHostData(
        host_id=str(HostId.generate()),
        host_name="failed-host",
        failure_reason="Build failed",
        created_at=now,
        updated_at=now,
    )
    fake_provider.mock_snapshots = [
        SnapshotInfo(
            id=SnapshotId("snap-test"),
            name=SnapshotName("should-not-matter"),
            created_at=datetime.now(timezone.utc),
        )
    ]
    failed_host = make_offline_host(certified_data, fake_provider, temp_mng_ctx)

    state = failed_host.get_state()
    assert state == HostState.FAILED


@pytest.mark.parametrize(
    "stop_reason,expected_state",
    [
        (HostState.PAUSED.value, HostState.PAUSED),
        (HostState.STOPPED.value, HostState.STOPPED),
        (None, HostState.CRASHED),
    ],
    ids=["paused", "stopped", "crashed_no_stop_reason"],
)
def test_get_state_based_on_stop_reason(
    fake_provider: MockProviderInstance,
    temp_mng_ctx: MngContext,
    stop_reason: str | None,
    expected_state: HostState,
) -> None:
    """Test that get_state returns the correct state based on stop_reason."""
    now = datetime.now(timezone.utc)
    certified_data = CertifiedHostData(
        host_id=str(HostId.generate()),
        host_name="test-host",
        stop_reason=stop_reason,
        created_at=now,
        updated_at=now,
    )
    fake_provider.mock_snapshots = [
        SnapshotInfo(
            id=SnapshotId("snap-test"),
            name=SnapshotName("snapshot"),
            created_at=datetime.now(timezone.utc),
        )
    ]
    host = make_offline_host(certified_data, fake_provider, temp_mng_ctx)

    state = host.get_state()
    assert state == expected_state


# =============================================================================
# Tests for validate_and_create_agent_reference standalone function
# =============================================================================


def test_validate_and_create_agent_reference_creates_valid_ref() -> None:
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    provider_name = ProviderInstanceName("test-provider")
    agent_data = {
        "id": str(agent_id),
        "name": "my-agent",
        "type": "claude",
        "permissions": ["read"],
    }

    ref = validate_and_create_agent_reference(agent_data, host_id, provider_name)

    assert ref is not None
    assert ref.agent_id == agent_id
    assert ref.agent_name == AgentName("my-agent")
    assert ref.host_id == host_id
    assert ref.provider_name == provider_name
    assert ref.certified_data == agent_data
    assert ref.agent_type == "claude"
    assert ref.permissions == ("read",)


def test_validate_and_create_agent_reference_returns_none_for_missing_id() -> None:
    host_id = HostId.generate()
    agent_data = {"name": "my-agent"}
    ref = validate_and_create_agent_reference(agent_data, host_id, ProviderInstanceName("p"))
    assert ref is None


def test_validate_and_create_agent_reference_returns_none_for_invalid_id() -> None:
    host_id = HostId.generate()
    agent_data = {"id": "not-a-valid-id", "name": "my-agent"}
    ref = validate_and_create_agent_reference(agent_data, host_id, ProviderInstanceName("p"))
    assert ref is None


def test_validate_and_create_agent_reference_returns_none_for_missing_name() -> None:
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    agent_data = {"id": str(agent_id)}
    ref = validate_and_create_agent_reference(agent_data, host_id, ProviderInstanceName("p"))
    assert ref is None


# =============================================================================
# Tests for default load_agent_refs on the provider
# =============================================================================


def test_load_agent_refs_default_returns_agents_grouped_by_host(
    fake_provider: MockProviderInstance, temp_mng_ctx: MngContext
) -> None:
    """Default load_agent_refs lists hosts and gets agent references in parallel."""
    host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    certified_data = CertifiedHostData(
        host_id=str(host_id),
        host_name="test-host",
        created_at=now,
        updated_at=now,
    )
    agent_id = AgentId.generate()
    fake_provider.mock_agent_data = [
        {"id": str(agent_id), "name": "agent-one", "type": "claude"},
    ]
    offline_host = make_offline_host(certified_data, fake_provider, temp_mng_ctx)
    fake_provider.mock_hosts = [offline_host]

    result = fake_provider.load_agent_refs(cg=temp_mng_ctx.concurrency_group)

    assert len(result) == 1
    host_ref = next(iter(result.keys()))
    assert host_ref.host_id == host_id
    assert host_ref.host_name == HostName("test-host")
    assert host_ref.provider_name == fake_provider.name

    agent_refs = result[host_ref]
    assert len(agent_refs) == 1
    assert agent_refs[0].agent_id == agent_id
    assert agent_refs[0].agent_name == AgentName("agent-one")


def test_load_agent_refs_default_returns_empty_for_no_hosts(
    fake_provider: MockProviderInstance, temp_mng_ctx: MngContext
) -> None:
    """Default load_agent_refs returns empty dict when provider has no hosts."""
    fake_provider.mock_hosts = []

    result = fake_provider.load_agent_refs(cg=temp_mng_ctx.concurrency_group)

    assert result == {}
