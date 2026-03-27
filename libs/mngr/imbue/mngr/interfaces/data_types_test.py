import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from pathlib import PurePosixPath

import pytest

from imbue.mngr.errors import InvalidRelativePathError
from imbue.mngr.interfaces.data_types import ActivityConfig
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CpuResources
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.interfaces.data_types import RelativePath
from imbue.mngr.interfaces.data_types import SSHInfo
from imbue.mngr.interfaces.data_types import get_activity_sources_for_idle_mode
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ProviderInstanceName


def test_relative_path_accepts_relative_string() -> None:
    path = RelativePath("some/relative/path.txt")
    assert str(path) == "some/relative/path.txt"


def test_relative_path_accepts_relative_path_object() -> None:
    path = RelativePath(Path("some/relative/path.txt"))
    assert str(path) == "some/relative/path.txt"


def test_relative_path_rejects_absolute_path_string() -> None:
    with pytest.raises(InvalidRelativePathError, match="Path must be relative"):
        RelativePath("/absolute/path.txt")


def test_relative_path_rejects_absolute_path_object() -> None:
    with pytest.raises(InvalidRelativePathError, match="Path must be relative"):
        RelativePath(Path("/absolute/path.txt"))


def test_relative_path_is_pure_posix_path() -> None:
    relative_path = RelativePath("some/path.txt")
    assert isinstance(relative_path, PurePosixPath)
    assert relative_path.parent == PurePosixPath("some")
    assert relative_path.name == "path.txt"
    assert relative_path.suffix == ".txt"


def test_relative_path_works_with_path_division() -> None:
    work_dir = Path("/home/user/work")
    relative_path = RelativePath(".claude/config.json")
    result = work_dir / relative_path
    assert result == Path("/home/user/work/.claude/config.json")


# =============================================================================
# SSHInfo Tests
# =============================================================================


def test_ssh_info_basic_creation() -> None:
    """Test that SSHInfo can be created with required fields."""
    ssh_info = SSHInfo(
        user="root",
        host="example.com",
        port=22,
        key_path=Path("/home/user/.ssh/id_rsa"),
        command="ssh -i /home/user/.ssh/id_rsa -p 22 root@example.com",
    )
    assert ssh_info.user == "root"
    assert ssh_info.host == "example.com"
    assert ssh_info.port == 22
    assert ssh_info.key_path == Path("/home/user/.ssh/id_rsa")
    assert ssh_info.command == "ssh -i /home/user/.ssh/id_rsa -p 22 root@example.com"


def test_ssh_info_custom_port() -> None:
    """Test SSHInfo with a custom port."""
    ssh_info = SSHInfo(
        user="deploy",
        host="192.168.1.100",
        port=2222,
        key_path=Path("/keys/deploy.pem"),
        command="ssh -i /keys/deploy.pem -p 2222 deploy@192.168.1.100",
    )
    assert ssh_info.port == 2222


def test_ssh_info_serialization() -> None:
    """Test that SSHInfo serializes to JSON correctly."""
    ssh_info = SSHInfo(
        user="root",
        host="example.com",
        port=22,
        key_path=Path("/home/user/.ssh/id_rsa"),
        command="ssh -i /home/user/.ssh/id_rsa -p 22 root@example.com",
    )
    data = ssh_info.model_dump(mode="json")
    assert data["user"] == "root"
    assert data["host"] == "example.com"
    assert data["port"] == 22
    assert data["key_path"] == "/home/user/.ssh/id_rsa"
    assert data["command"] == "ssh -i /home/user/.ssh/id_rsa -p 22 root@example.com"


# =============================================================================
# HostDetails Extended Fields Tests
# =============================================================================


def test_host_details_minimal_creation() -> None:
    """Test that HostDetails can be created with minimal required fields."""
    host_details = HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    assert host_details.name == "test-host"
    assert host_details.provider_name == ProviderInstanceName("local")
    # Extended fields should be None/empty by default
    assert host_details.state is None
    assert host_details.image is None
    assert host_details.tags == {}
    assert host_details.boot_time is None
    assert host_details.uptime_seconds is None
    assert host_details.resource is None
    assert host_details.ssh is None
    assert host_details.snapshots == []


def test_host_details_with_extended_fields() -> None:
    """Test that HostDetails can be created with all extended fields."""
    boot_time = datetime.now(timezone.utc)
    ssh_info = SSHInfo(
        user="root",
        host="example.com",
        port=22,
        key_path=Path("/home/user/.ssh/id_rsa"),
        command="ssh -i /home/user/.ssh/id_rsa -p 22 root@example.com",
    )
    resources = HostResources(cpu=CpuResources(count=4), memory_gb=16.0, disk_gb=100.0)

    host_details = HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("docker"),
        state=HostState.RUNNING,
        image="ubuntu:22.04",
        tags={"env": "production", "team": "infra"},
        boot_time=boot_time,
        uptime_seconds=3600.5,
        resource=resources,
        ssh=ssh_info,
        # Note: not testing snapshots here as SnapshotInfo has complex ID requirements
    )

    assert host_details.state == HostState.RUNNING
    assert host_details.image == "ubuntu:22.04"
    assert host_details.tags == {"env": "production", "team": "infra"}
    assert host_details.boot_time == boot_time
    assert host_details.uptime_seconds == 3600.5
    assert host_details.resource is not None
    assert host_details.resource.memory_gb == 16.0
    assert host_details.ssh is not None
    assert host_details.ssh.user == "root"
    # Snapshots should be empty by default
    assert host_details.snapshots == []


def test_host_details_serialization_with_extended_fields() -> None:
    """Test that HostDetails with extended fields serializes correctly."""
    boot_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    ssh_info = SSHInfo(
        user="root",
        host="example.com",
        port=22,
        key_path=Path("/keys/id_rsa"),
        command="ssh -i /keys/id_rsa -p 22 root@example.com",
    )

    host_details = HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("modal"),
        state=HostState.RUNNING,
        image="custom-image:v1",
        tags={"key": "value"},
        boot_time=boot_time,
        uptime_seconds=7200.0,
        ssh=ssh_info,
    )

    data = host_details.model_dump(mode="json")

    assert data["state"] == HostState.RUNNING.value
    assert data["image"] == "custom-image:v1"
    assert data["tags"] == {"key": "value"}
    assert data["uptime_seconds"] == 7200.0
    assert data["ssh"]["user"] == "root"
    assert data["ssh"]["port"] == 22


# =============================================================================
# CertifiedHostData Tests
# =============================================================================


def test_certified_host_data_tmux_session_prefix_defaults_to_none() -> None:
    """tmux_session_prefix should default to None for backward compatibility."""
    now = datetime.now(timezone.utc)
    data = CertifiedHostData(host_id="host-123", host_name="test-host", created_at=now, updated_at=now)
    assert data.tmux_session_prefix is None


def test_certified_host_data_tmux_session_prefix_set() -> None:
    """tmux_session_prefix should be settable."""
    now = datetime.now(timezone.utc)
    data = CertifiedHostData(
        host_id="host-123",
        host_name="test-host",
        tmux_session_prefix="mngr-",
        created_at=now,
        updated_at=now,
    )
    assert data.tmux_session_prefix == "mngr-"


def test_certified_host_data_tmux_session_prefix_serializes_to_json() -> None:
    """tmux_session_prefix should round-trip through JSON serialization."""
    now = datetime.now(timezone.utc)
    data = CertifiedHostData(
        host_id="host-123",
        host_name="test-host",
        tmux_session_prefix="custom-prefix-",
        created_at=now,
        updated_at=now,
    )
    json_str = json.dumps(data.model_dump(by_alias=True, mode="json"))
    parsed = json.loads(json_str)
    assert parsed["tmux_session_prefix"] == "custom-prefix-"

    # Deserialize back
    restored = CertifiedHostData.model_validate(parsed)
    assert restored.tmux_session_prefix == "custom-prefix-"


def test_certified_host_data_backward_compatible_without_prefix() -> None:
    """CertifiedHostData should deserialize from JSON without tmux_session_prefix."""
    data = CertifiedHostData.model_validate({"host_id": "host-123", "host_name": "test-host"})
    assert data.tmux_session_prefix is None


# =============================================================================
# IdleMode derivation consistency tests
# =============================================================================


_IDLE_MODES_WITH_KNOWN_SOURCES = [m for m in IdleMode if m != IdleMode.CUSTOM]


@pytest.mark.parametrize("mode", _IDLE_MODES_WITH_KNOWN_SOURCES)
def test_activity_config_idle_mode_matches_hosts_common_mapping(mode: IdleMode) -> None:
    """ActivityConfig.idle_mode must agree with get_activity_sources_for_idle_mode.

    The mapping in interfaces/data_types.py and the one in hosts/common.py must
    stay in sync. This test verifies the round-trip: forward-mapping a mode to
    its activity sources, then deriving idle_mode from those sources, must
    return the original mode.
    """
    sources = get_activity_sources_for_idle_mode(mode)
    config = ActivityConfig(idle_timeout_seconds=600, activity_sources=sources)
    assert config.idle_mode == mode


@pytest.mark.parametrize("mode", _IDLE_MODES_WITH_KNOWN_SOURCES)
def test_certified_host_data_idle_mode_matches_hosts_common_mapping(mode: IdleMode) -> None:
    """CertifiedHostData.idle_mode must agree with get_activity_sources_for_idle_mode."""
    now = datetime.now(timezone.utc)
    sources = get_activity_sources_for_idle_mode(mode)
    data = CertifiedHostData(
        host_id="host-sync-test",
        host_name="sync-test",
        activity_sources=sources,
        created_at=now,
        updated_at=now,
    )
    assert data.idle_mode == mode


def test_certified_host_data_strips_idle_mode_from_old_json() -> None:
    """Old data.json files containing idle_mode should deserialize without error."""
    old_json = {
        "host_id": "host-old",
        "host_name": "old-host",
        "idle_mode": "IO",
        "idle_timeout_seconds": 900,
    }
    data = CertifiedHostData.model_validate(old_json)
    assert data.idle_mode == IdleMode.IO
    assert data.idle_timeout_seconds == 900


# =============================================================================
# CertifiedHostData created_at / updated_at Tests
# =============================================================================


def test_certified_host_data_created_at_and_updated_at_explicit() -> None:
    """created_at and updated_at should be settable explicitly."""
    now = datetime(2026, 2, 15, 12, 0, 0, tzinfo=timezone.utc)
    data = CertifiedHostData(
        host_id="host-ts-1",
        host_name="ts-host",
        created_at=now,
        updated_at=now,
    )
    assert data.created_at == now
    assert data.updated_at == now


def test_certified_host_data_timestamps_backward_compat_from_json() -> None:
    """Old data.json without created_at/updated_at should deserialize with defaults."""
    old_json = {
        "host_id": "host-old-ts",
        "host_name": "old-host",
    }
    before = datetime.now(timezone.utc)
    data = CertifiedHostData.model_validate(old_json)

    # created_at defaults to ~1 week ago
    assert data.created_at < before - timedelta(days=6)
    assert data.created_at > before - timedelta(days=8)

    # updated_at defaults to ~1 day ago
    assert data.updated_at < before
    assert data.updated_at > before - timedelta(days=2)


def test_certified_host_data_timestamps_round_trip_through_json() -> None:
    """created_at and updated_at should survive JSON serialization round-trip."""
    now = datetime(2026, 2, 15, 10, 30, 0, tzinfo=timezone.utc)
    data = CertifiedHostData(
        host_id="host-rt-1",
        host_name="rt-host",
        created_at=now,
        updated_at=now,
    )
    json_str = json.dumps(data.model_dump(by_alias=True, mode="json"))
    parsed = json.loads(json_str)
    restored = CertifiedHostData.model_validate(parsed)

    assert restored.created_at == now
    assert restored.updated_at == now


def test_certified_host_data_timestamps_are_utc() -> None:
    """Timestamps should be timezone-aware UTC datetimes."""
    now = datetime.now(timezone.utc)
    data = CertifiedHostData(
        host_id="host-utc-1",
        host_name="utc-host",
        created_at=now,
        updated_at=now,
    )
    assert data.created_at.tzinfo is not None
    assert data.updated_at.tzinfo is not None
