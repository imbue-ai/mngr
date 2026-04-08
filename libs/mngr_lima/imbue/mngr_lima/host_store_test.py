from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.local.volume import LocalVolume
from imbue.mngr_lima.host_store import HostRecord
from imbue.mngr_lima.host_store import LimaHostConfig
from imbue.mngr_lima.host_store import LimaHostStore


def _make_certified_data(host_id: HostId, host_name: str = "test-host") -> CertifiedHostData:
    now = datetime.now(timezone.utc)
    return CertifiedHostData(
        host_id=str(host_id),
        host_name=host_name,
        user_tags={},
        snapshots=[],
        created_at=now,
        updated_at=now,
    )


def _make_store(tmp_path: Path) -> LimaHostStore:
    volume = LocalVolume(root_path=tmp_path)
    return LimaHostStore(volume=volume)


def test_write_and_read_host_record(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    host_id = HostId.generate()
    certified_data = _make_certified_data(host_id)

    record = HostRecord(
        certified_host_data=certified_data,
        ssh_hostname="127.0.0.1",
        ssh_port=60022,
        ssh_user="josh",
        ssh_identity_file="/home/josh/.lima/_config/user",
        config=LimaHostConfig(instance_name="mngr-test"),
    )

    store.write_host_record(record)
    loaded = store.read_host_record(host_id)

    assert loaded is not None
    assert loaded.ssh_hostname == "127.0.0.1"
    assert loaded.ssh_port == 60022
    assert loaded.ssh_user == "josh"
    assert loaded.config is not None
    assert loaded.config.instance_name == "mngr-test"


def test_read_nonexistent_record(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store.read_host_record(HostId.generate()) is None


def test_delete_host_record(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    host_id = HostId.generate()
    certified_data = _make_certified_data(host_id)

    record = HostRecord(certified_host_data=certified_data)
    store.write_host_record(record)
    assert store.read_host_record(host_id) is not None

    store.delete_host_record(host_id)
    assert store.read_host_record(host_id, use_cache=False) is None


def test_list_all_host_records(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    ids = [HostId.generate() for _ in range(3)]
    for host_id in ids:
        certified_data = _make_certified_data(host_id)
        store.write_host_record(HostRecord(certified_host_data=certified_data))

    records = store.list_all_host_records()
    assert len(records) == 3
    record_ids = {r.certified_host_data.host_id for r in records}
    assert record_ids == {str(h) for h in ids}


def test_persist_and_list_agent_data(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    host_id = HostId.generate()
    agent_id = AgentId.generate()

    agent_data = {"id": str(agent_id), "name": "test-agent", "type": "claude"}
    store.persist_agent_data(host_id, agent_data)

    records = store.list_persisted_agent_data_for_host(host_id)
    assert len(records) == 1
    assert records[0]["id"] == str(agent_id)
    assert records[0]["name"] == "test-agent"


def test_remove_persisted_agent_data(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    host_id = HostId.generate()
    agent_id = AgentId.generate()

    store.persist_agent_data(host_id, {"id": str(agent_id), "name": "agent"})
    assert len(store.list_persisted_agent_data_for_host(host_id)) == 1

    store.remove_persisted_agent_data(host_id, agent_id)
    assert len(store.list_persisted_agent_data_for_host(host_id)) == 0


def test_cache_behavior(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    host_id = HostId.generate()
    certified_data = _make_certified_data(host_id)

    record = HostRecord(certified_host_data=certified_data, ssh_port=100)
    store.write_host_record(record)

    # Should hit cache
    cached = store.read_host_record(host_id)
    assert cached is not None
    assert cached.ssh_port == 100

    # Clear cache and re-read from disk
    store.clear_cache()
    from_disk = store.read_host_record(host_id)
    assert from_disk is not None
    assert from_disk.ssh_port == 100
