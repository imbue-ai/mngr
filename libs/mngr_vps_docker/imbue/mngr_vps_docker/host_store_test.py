"""Tests for VPS Docker host store data types."""

from datetime import datetime
from datetime import timezone

from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord
from imbue.mngr_vps_docker.host_store import VpsHostConfig
from imbue.mngr_vps_docker.primitives import VpsInstanceId


def _make_certified_data(host_id: str = "test-host-123", host_name: str = "test-host") -> CertifiedHostData:
    """Create a minimal CertifiedHostData for testing."""
    now = datetime.now(timezone.utc)
    return CertifiedHostData(
        host_id=host_id,
        host_name=host_name,
        idle_timeout_seconds=800,
        activity_sources=(),
        image="debian:bookworm-slim",
        user_tags={},
        created_at=now,
        updated_at=now,
    )


def test_vps_host_config_creation() -> None:
    config = VpsHostConfig(
        vps_instance_id=VpsInstanceId("inst-abc123"),
        region="ewr",
        plan="vc2-1c-1gb",
        os_id=2136,
        container_name="mngr-test-host",
        volume_name="mngr-host-vol-abc123",
    )
    assert config.vps_instance_id == VpsInstanceId("inst-abc123")
    assert config.region == "ewr"
    assert config.plan == "vc2-1c-1gb"
    assert config.os_id == 2136
    assert config.container_name == "mngr-test-host"
    assert config.volume_name == "mngr-host-vol-abc123"


def test_vps_host_config_optional_fields() -> None:
    config = VpsHostConfig(
        vps_instance_id=VpsInstanceId("inst-abc123"),
        region="ewr",
        plan="vc2-1c-1gb",
        os_id=2136,
        container_name="test",
        volume_name="vol",
    )
    assert config.start_args == ()
    assert config.image is None
    assert config.vps_ssh_key_id is None


def test_vps_docker_host_record_creation() -> None:
    certified_data = _make_certified_data()
    record = VpsDockerHostRecord(
        certified_host_data=certified_data,
        vps_ip="192.168.1.100",
        ssh_host_public_key="ssh-ed25519 AAAA vps-host-key",
        container_ssh_host_public_key="ssh-ed25519 BBBB container-host-key",
    )
    assert record.certified_host_data.host_id == "test-host-123"
    assert record.vps_ip == "192.168.1.100"
    assert record.ssh_host_public_key == "ssh-ed25519 AAAA vps-host-key"


def test_vps_docker_host_record_optional_fields() -> None:
    certified_data = _make_certified_data()
    record = VpsDockerHostRecord(certified_host_data=certified_data)
    assert record.vps_ip is None
    assert record.ssh_host_public_key is None
    assert record.container_ssh_host_public_key is None
    assert record.config is None
    assert record.container_id is None


def test_vps_docker_host_record_serialization_roundtrip() -> None:
    certified_data = _make_certified_data()
    config = VpsHostConfig(
        vps_instance_id=VpsInstanceId("inst-abc123"),
        region="ewr",
        plan="vc2-1c-1gb",
        os_id=2136,
        container_name="test",
        volume_name="vol",
    )
    original = VpsDockerHostRecord(
        certified_host_data=certified_data,
        vps_ip="10.0.0.1",
        config=config,
        container_id="deadbeef1234",
    )

    # Serialize and deserialize
    json_str = original.model_dump_json()
    restored = VpsDockerHostRecord.model_validate_json(json_str)

    assert restored.certified_host_data.host_id == "test-host-123"
    assert restored.vps_ip == "10.0.0.1"
    assert restored.config is not None
    assert restored.config.vps_instance_id == VpsInstanceId("inst-abc123")
    assert restored.container_id == "deadbeef1234"


def test_vps_docker_host_record_model_copy() -> None:
    certified_data = _make_certified_data()
    record = VpsDockerHostRecord(
        certified_host_data=certified_data,
        vps_ip="10.0.0.1",
    )
    new_data = _make_certified_data(host_name="updated-host")
    updated = record.model_copy(update={"certified_host_data": new_data})
    assert updated.certified_host_data.host_name == "updated-host"
    assert updated.vps_ip == "10.0.0.1"
    # Original unchanged
    assert record.certified_host_data.host_name == "test-host"
