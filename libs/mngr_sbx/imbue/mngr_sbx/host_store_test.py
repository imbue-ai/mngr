"""Unit tests for the sbx host record store and naming helpers."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.local.volume import LocalVolume
from imbue.mngr_sbx.host_store import HostRecord
from imbue.mngr_sbx.host_store import SbxHostConfig
from imbue.mngr_sbx.host_store import SbxHostStore
from imbue.mngr_sbx.host_store import host_name_from_sandbox_name
from imbue.mngr_sbx.host_store import sandbox_name_for_host


def _make_certified_data(host_id: HostId, host_name: str) -> CertifiedHostData:
    now = datetime.now(timezone.utc)
    return CertifiedHostData(
        host_id=str(host_id),
        host_name=host_name,
        user_tags={},
        snapshots=[],
        created_at=now,
        updated_at=now,
    )


def _make_store(tmp_path: Path) -> SbxHostStore:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return SbxHostStore(volume=LocalVolume(root_path=state_dir))


def test_sandbox_name_for_host_prepends_prefix() -> None:
    assert sandbox_name_for_host("alpha", "mngr-") == "mngr-alpha"


def test_host_name_from_sandbox_name_strips_matching_prefix() -> None:
    assert host_name_from_sandbox_name("mngr-alpha", "mngr-") == "alpha"


def test_host_name_from_sandbox_name_returns_none_when_prefix_missing() -> None:
    assert host_name_from_sandbox_name("other-alpha", "mngr-") is None


def test_host_name_from_sandbox_name_returns_none_when_only_prefix() -> None:
    assert host_name_from_sandbox_name("mngr-", "mngr-") is None


def test_sbx_host_store_round_trip_record(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    host_id = HostId.generate()
    record = HostRecord(
        certified_host_data=_make_certified_data(host_id, "alpha"),
        ssh_hostname="127.0.0.1",
        ssh_port=32770,
        ssh_user="root",
        ssh_identity_file="/tmp/key",
        ssh_host_public_key="ssh-ed25519 AAAA",
        config=SbxHostConfig(
            sandbox_name="mngr-alpha",
            agent_type="docker-agent",
            workspace_path="/repo",
        ),
    )

    store.write_host_record(record)
    read_back = store.read_host_record(host_id, use_cache=False)

    assert read_back is not None
    assert read_back.certified_host_data.host_name == "alpha"
    assert read_back.config is not None
    assert read_back.config.sandbox_name == "mngr-alpha"
    assert read_back.ssh_port == 32770


def test_sbx_host_store_read_returns_none_for_missing_host(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store.read_host_record(HostId.generate(), use_cache=False) is None


def test_sbx_host_store_list_all_host_records_includes_written_record(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    host_id = HostId.generate()
    record = HostRecord(certified_host_data=_make_certified_data(host_id, "alpha"))
    store.write_host_record(record)

    records = store.list_all_host_records()
    host_names = [r.certified_host_data.host_name for r in records]
    assert "alpha" in host_names


def test_sbx_host_store_delete_host_record_removes_it(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    host_id = HostId.generate()
    store.write_host_record(HostRecord(certified_host_data=_make_certified_data(host_id, "alpha")))

    store.delete_host_record(host_id)

    assert store.read_host_record(host_id, use_cache=False) is None


def test_sbx_host_store_persist_and_remove_agent_data_round_trip(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    host_id = HostId.generate()
    agent_id = AgentId.generate()

    store.persist_agent_data(host_id, {"id": str(agent_id), "name": "agent-x"})
    persisted = store.list_persisted_agent_data_for_host(host_id)
    assert any(record.get("name") == "agent-x" for record in persisted)

    store.remove_persisted_agent_data(host_id, agent_id)
    assert store.list_persisted_agent_data_for_host(host_id) == []
