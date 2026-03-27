from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.docker.host_store import ContainerConfig
from imbue.mngr.providers.docker.host_store import DockerHostStore
from imbue.mngr.providers.docker.host_store import HostRecord
from imbue.mngr.providers.local.volume import LocalVolume

HOST_ID_A = "host-00000000000000000000000000000001"
HOST_ID_B = "host-00000000000000000000000000000002"
HOST_ID_C = "host-00000000000000000000000000000003"
AGENT_ID_A = "agent-00000000000000000000000000000001"


def _make_host_record(
    host_id: str = HOST_ID_A,
    host_name: str = "test-host",
    ssh_host: str = "127.0.0.1",
    ssh_port: int = 12345,
    ssh_host_public_key: str = "ssh-ed25519 AAAA",
) -> HostRecord:
    now = datetime.now(timezone.utc)
    return HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=host_id,
            host_name=host_name,
            created_at=now,
            updated_at=now,
        ),
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        ssh_host_public_key=ssh_host_public_key,
        config=ContainerConfig(start_args=("--cpus=2", "--memory=4g")),
        container_id="abc123def456",
    )


@pytest.fixture
def store(tmp_path: Path) -> DockerHostStore:
    volume = LocalVolume(root_path=tmp_path / "docker-store")
    return DockerHostStore(volume=volume)


def test_write_and_read_host_record(store: DockerHostStore) -> None:
    record = _make_host_record()
    store.write_host_record(record)

    result = store.read_host_record(HostId(HOST_ID_A))
    assert result is not None
    assert result.certified_host_data.host_id == HOST_ID_A
    assert result.certified_host_data.host_name == "test-host"
    assert result.ssh_host == "127.0.0.1"
    assert result.ssh_port == 12345
    assert result.ssh_host_public_key == "ssh-ed25519 AAAA"
    assert result.config is not None
    assert result.config.start_args == ("--cpus=2", "--memory=4g")
    assert result.container_id == "abc123def456"


def test_read_host_record_returns_none_for_nonexistent(store: DockerHostStore) -> None:
    result = store.read_host_record(HostId(HOST_ID_B))
    assert result is None


def test_read_host_record_caching(store: DockerHostStore) -> None:
    record = _make_host_record()
    store.write_host_record(record)

    result1 = store.read_host_record(HostId(HOST_ID_A))
    assert result1 is not None

    result2 = store.read_host_record(HostId(HOST_ID_A))
    assert result2 is result1


def test_delete_host_record(store: DockerHostStore) -> None:
    record = _make_host_record()
    store.write_host_record(record)

    store.delete_host_record(HostId(HOST_ID_A))

    result = store.read_host_record(HostId(HOST_ID_A), use_cache=False)
    assert result is None


def test_delete_host_record_nonexistent_is_noop(store: DockerHostStore) -> None:
    store.delete_host_record(HostId(HOST_ID_B))


def test_list_all_host_records_empty(store: DockerHostStore) -> None:
    result = store.list_all_host_records()
    assert result == []


def test_list_all_host_records_returns_all_records(store: DockerHostStore) -> None:
    record1 = _make_host_record(host_id=HOST_ID_A, host_name="host-one")
    record2 = _make_host_record(host_id=HOST_ID_B, host_name="host-two")
    store.write_host_record(record1)
    store.write_host_record(record2)

    results = store.list_all_host_records()
    assert len(results) == 2
    host_ids = {r.certified_host_data.host_id for r in results}
    assert host_ids == {HOST_ID_A, HOST_ID_B}


def test_list_all_host_records_skips_corrupt_files(store: DockerHostStore, tmp_path: Path) -> None:
    record = _make_host_record(host_id=HOST_ID_A, host_name="valid")
    store.write_host_record(record)

    # Write corrupt data directly via the volume
    store.volume.write_files({f"host_state/{HOST_ID_B}.json": b"not valid json {{{"})

    results = store.list_all_host_records()
    assert len(results) == 1
    assert results[0].certified_host_data.host_id == HOST_ID_A


def test_persist_agent_data(store: DockerHostStore) -> None:
    host_id = HostId(HOST_ID_A)
    agent_data = {"id": AGENT_ID_A, "name": "test-agent", "type": "echo"}

    store.persist_agent_data(host_id, agent_data)

    results = store.list_persisted_agent_data_for_host(host_id)
    assert len(results) == 1
    assert results[0]["id"] == AGENT_ID_A
    assert results[0]["name"] == "test-agent"


def test_persist_agent_data_without_id_is_noop(store: DockerHostStore) -> None:
    host_id = HostId(HOST_ID_A)
    agent_data: dict[str, object] = {"name": "no-id-agent"}

    store.persist_agent_data(host_id, agent_data)

    results = store.list_persisted_agent_data_for_host(host_id)
    assert len(results) == 0


def test_list_persisted_agent_data_for_host_empty(store: DockerHostStore) -> None:
    results = store.list_persisted_agent_data_for_host(HostId(HOST_ID_A))
    assert results == []


def test_remove_persisted_agent_data(store: DockerHostStore) -> None:
    host_id = HostId(HOST_ID_A)
    agent_id = AgentId(AGENT_ID_A)
    agent_data = {"id": str(agent_id), "name": "test-agent"}

    store.persist_agent_data(host_id, agent_data)
    assert len(store.list_persisted_agent_data_for_host(host_id)) == 1

    store.remove_persisted_agent_data(host_id, agent_id)
    assert len(store.list_persisted_agent_data_for_host(host_id)) == 0


def test_remove_persisted_agent_data_nonexistent_is_noop(store: DockerHostStore) -> None:
    store.remove_persisted_agent_data(HostId(HOST_ID_A), AgentId(AGENT_ID_A))


def test_clear_cache(store: DockerHostStore) -> None:
    record = _make_host_record()
    store.write_host_record(record)

    result1 = store.read_host_record(HostId(HOST_ID_A))
    assert result1 is not None

    store.clear_cache()

    result2 = store.read_host_record(HostId(HOST_ID_A))
    assert result2 is not None
    assert result2 is not result1
    assert result2.certified_host_data.host_id == result1.certified_host_data.host_id
