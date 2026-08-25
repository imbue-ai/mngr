import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.errors import HostRecordUnreadableError
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


@pytest.mark.allow_warnings(match=r"^Failed to parse host record host_state")
def test_read_corrupt_record_reads_as_missing_by_default(tmp_path: Path) -> None:
    """Default (non-strict) behavior: an unparseable record is treated like an absent one."""
    store = _make_store(tmp_path)
    host_id = HostId.generate()
    store.volume.write_files({f"host_state/{host_id}.json": b"not valid json {{{"})

    assert store.read_host_record(host_id, use_cache=False) is None


def test_read_corrupt_record_raises_in_strict_mode(tmp_path: Path) -> None:
    """Strict parsing distinguishes corrupt from absent: corrupt raises, absent stays None."""
    store = LimaHostStore(volume=LocalVolume(root_path=tmp_path), is_strict_parsing=True)
    host_id = HostId.generate()
    store.volume.write_files({f"host_state/{host_id}.json": b"not valid json {{{"})

    with pytest.raises(HostRecordUnreadableError, match=str(host_id)):
        store.read_host_record(host_id, use_cache=False)
    assert store.read_host_record(HostId.generate(), use_cache=False) is None


def test_read_host_record_recovers_when_torn_write_completes_before_retries_exhaust(tmp_path: Path) -> None:
    """A torn (empty) read heals via retry: the re-read picks up the completed write."""
    volume = _HealingReadVolume(root_path=tmp_path)
    store = LimaHostStore(volume=volume, is_strict_parsing=True)
    host_id = HostId.generate()
    store.write_host_record(HostRecord(certified_host_data=_make_certified_data(host_id)))
    store.clear_cache()

    # First read returns b"" (a torn mid-write observation); the retry re-reads
    # the real content, so even strict mode sees a valid record.
    volume.torn_reads_remaining = 1
    result = store.read_host_record(host_id, use_cache=False)
    assert result is not None
    assert result.certified_host_data.host_id == str(host_id)


class _HealingReadVolume(LocalVolume):
    """LocalVolume whose next ``torn_reads_remaining`` reads observe an empty file."""

    torn_reads_remaining: int = 0

    def read_file(self, path: str) -> bytes:
        if self.torn_reads_remaining > 0:
            self.torn_reads_remaining -= 1
            return b""
        return super().read_file(path)


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


def test_lima_host_config_default_layout_is_bind_mount() -> None:
    """A newly-constructed LimaHostConfig defaults is_host_data_volume_exposed
    to True so that hosts created before this field existed continue to behave
    exactly as they did before."""
    config = LimaHostConfig(instance_name="mngr-test")
    assert config.is_host_data_volume_exposed is True
    assert config.host_data_disk_name is None


def test_lima_host_config_btrfs_mode_round_trips(tmp_path: Path) -> None:
    """btrfs-mode hosts persist their disk name and the False flag, and the
    values survive a write/read cycle through the host store."""
    store = _make_store(tmp_path)
    host_id = HostId.generate()
    record = HostRecord(
        certified_host_data=_make_certified_data(host_id),
        config=LimaHostConfig(
            instance_name="mngr-btrfs-test",
            is_host_data_volume_exposed=False,
            host_data_disk_name="mngr-abc123-data",
        ),
    )
    store.write_host_record(record)

    store.clear_cache()
    loaded = store.read_host_record(host_id)
    assert loaded is not None
    assert loaded.config is not None
    assert loaded.config.is_host_data_volume_exposed is False
    assert loaded.config.host_data_disk_name == "mngr-abc123-data"


def test_lima_host_config_run_as_root_defaults_to_false() -> None:
    """A LimaHostConfig defaults to the non-root agent user, so pre-existing
    records (which lack the field) behave unchanged."""
    config = LimaHostConfig(instance_name="mngr-test")
    assert config.is_run_as_root is False


def test_lima_host_config_run_as_root_round_trips(tmp_path: Path) -> None:
    """run-as-root hosts persist the is_run_as_root flag alongside the btrfs
    layout, and the values survive a write/read cycle."""
    store = _make_store(tmp_path)
    host_id = HostId.generate()
    record = HostRecord(
        certified_host_data=_make_certified_data(host_id),
        config=LimaHostConfig(
            instance_name="mngr-root-test",
            is_host_data_volume_exposed=False,
            host_data_disk_name="mngr-abc123-data",
            is_run_as_root=True,
        ),
    )
    store.write_host_record(record)

    store.clear_cache()
    loaded = store.read_host_record(host_id)
    assert loaded is not None
    assert loaded.config is not None
    assert loaded.config.is_run_as_root is True


def test_lima_host_config_legacy_record_defaults_to_bind_mount(tmp_path: Path) -> None:
    """Records written before is_host_data_volume_exposed existed must
    deserialize with the field defaulting to True (today's behavior). We
    write the JSON shape an older mngr would have produced and assert."""
    store = _make_store(tmp_path)
    host_id = HostId.generate()
    legacy_json = {
        "certified_host_data": {
            "host_id": str(host_id),
            "host_name": "legacy-host",
            "user_tags": {},
            "snapshots": [],
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
        },
        "config": {
            "instance_name": "mngr-legacy",
            "start_args": [],
            "image_url": None,
        },
    }
    store.volume.write_files({f"host_state/{host_id}.json": json.dumps(legacy_json).encode("utf-8")})

    loaded = store.read_host_record(host_id, use_cache=False)
    assert loaded is not None
    assert loaded.config is not None
    assert loaded.config.is_host_data_volume_exposed is True
    assert loaded.config.host_data_disk_name is None


def test_host_record_without_creation_flag_deserializes_as_completed(tmp_path: Path) -> None:
    """Pre-change on-disk records (no is_creation_in_progress field) load as completed hosts."""
    raw = HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(HostId.generate()),
            host_name="legacy-host",
            user_tags={},
            snapshots=[],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        ),
    ).model_dump_json(exclude={"is_creation_in_progress"})
    assert "is_creation_in_progress" not in raw

    record = HostRecord.model_validate_json(raw)

    assert record.is_creation_in_progress is False
