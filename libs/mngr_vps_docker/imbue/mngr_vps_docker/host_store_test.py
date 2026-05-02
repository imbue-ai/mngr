"""Tests for VPS Docker host store data types."""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr_vps_docker.docker_over_ssh import DockerOverSsh
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord
from imbue.mngr_vps_docker.host_store import VpsDockerHostStore
from imbue.mngr_vps_docker.host_store import VpsHostConfig
from imbue.mngr_vps_docker.host_store import _FILE_SEP
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


# -- Batched read tests --


def _make_store() -> VpsDockerHostStore:
    """Create a VpsDockerHostStore with a dummy DockerOverSsh for testing parse methods."""
    dummy_ssh = DockerOverSsh(
        vps_ip="127.0.0.1",
        ssh_key_path=Path("/dev/null"),
        known_hosts_path=Path("/dev/null"),
    )
    return VpsDockerHostStore(
        docker_ssh=dummy_ssh,
        state_container_name="test-state",
    )


def test_split_batched_output_empty() -> None:
    store = _make_store()
    assert store._split_batched_output("") == []


def test_split_batched_output_single_file() -> None:
    store = _make_store()
    content = json.dumps({"host_id": "host-abc"})
    output = f"{_FILE_SEP}/mngr-state/host_state/host-abc.json\n{content}"
    result = store._split_batched_output(output)
    assert len(result) == 1
    assert result[0][0] == "/mngr-state/host_state/host-abc.json"
    assert json.loads(result[0][1])["host_id"] == "host-abc"


def test_split_batched_output_multiple_files() -> None:
    store = _make_store()
    content1 = json.dumps({"host_id": "host-1"})
    content2 = json.dumps({"host_id": "host-2"})
    output = (
        f"{_FILE_SEP}/mngr-state/host_state/host-1.json\n{content1}\n"
        f"{_FILE_SEP}/mngr-state/host_state/host-2.json\n{content2}"
    )
    result = store._split_batched_output(output)
    assert len(result) == 2


def test_parse_batched_json_files() -> None:
    store = _make_store()
    data1 = {"id": "agent-1", "name": "a1"}
    data2 = {"id": "agent-2", "name": "a2"}
    output = (
        f"{_FILE_SEP}/mngr-state/host_state/host-x/agent-1.json\n{json.dumps(data1)}\n"
        f"{_FILE_SEP}/mngr-state/host_state/host-x/agent-2.json\n{json.dumps(data2)}"
    )
    result = store._parse_batched_json_files(output)
    assert len(result) == 2
    assert result[0]["id"] == "agent-1"
    assert result[1]["id"] == "agent-2"


def test_parse_batched_json_files_raises_on_invalid() -> None:
    """A corrupt file in batched output raises so the corruption is visible rather than silently dropped."""
    store = _make_store()
    output = (
        f"{_FILE_SEP}/mngr-state/host_state/host-x/agent-1.json\n{{invalid json\n"
        f"{_FILE_SEP}/mngr-state/host_state/host-x/agent-2.json\n{json.dumps({'id': 'agent-2'})}"
    )
    with pytest.raises(json.JSONDecodeError):
        store._parse_batched_json_files(output)


def test_parse_batched_host_records() -> None:
    store = _make_store()
    host_id_str = "host-00112233445566778899aabbccddeeff"
    certified_data = _make_certified_data(host_id=host_id_str, host_name="my-host")
    record = VpsDockerHostRecord(certified_host_data=certified_data, vps_ip="10.0.0.1")
    output = f"{_FILE_SEP}/mngr-state/host_state/{host_id_str}.json\n{record.model_dump_json()}"
    result = store._parse_batched_host_records(output)
    assert len(result) == 1
    assert result[0].certified_host_data.host_id == host_id_str


def test_parse_batched_host_records_ignores_subdirectory_files() -> None:
    """Host records are top-level .json files, not files in subdirectories."""
    store = _make_store()
    agent_data = json.dumps({"id": "agent-1", "name": "a1"})
    output = f"{_FILE_SEP}/mngr-state/host_state/host-x/agent-1.json\n{agent_data}"
    result = store._parse_batched_host_records(output)
    assert len(result) == 0
