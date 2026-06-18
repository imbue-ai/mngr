"""Tests for the provider-agnostic host-state-store / host_dir-backend strategy objects."""

from collections.abc import Mapping
from datetime import datetime
from datetime import timezone

import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr_vps_docker.host_state_store import BucketHostStateStore
from imbue.mngr_vps_docker.host_state_store import NullHostDirBackend
from imbue.mngr_vps_docker.host_state_store import missing_state_bucket_error
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord


def test_null_host_dir_backend_is_no_op() -> None:
    """The fallback host_dir backend offers nothing: no identity, no volume, and no-op syncs.

    This is the half of the select-once strategy a provider uses when offline
    host_dir is off or no state bucket exists, so every method must degrade
    silently rather than raise.
    """
    backend = NullHostDirBackend()
    host_id = HostId.generate()
    assert backend.create_identity() is None
    assert backend.volume_reference(host_id) is None
    assert backend.volume(host_id) is None
    # The sync hooks are no-ops that must not raise.
    backend.install_sync(host_id=host_id, vps_ip="203.0.113.4")
    backend.trigger_final_sync(host_id, "203.0.113.4")


def _host_record(host_id: HostId, host_name: str) -> VpsDockerHostRecord:
    return VpsDockerHostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name=host_name,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            stop_reason=HostState.STOPPED.value,
        )
    )


class _FakeBucketError(MngrError):
    pass


class _FakeBucket:
    """Minimal ``StateBucket`` whose host-record read/write is scriptable (value or raise)."""

    def __init__(
        self, *, record_json: str | None = None, raise_on_read: bool = False, raise_on_write: bool = False
    ) -> None:
        self._record_json = record_json
        self._raise_on_read = raise_on_read
        self._raise_on_write = raise_on_write

    def write_host_record_json(self, host_id: HostId, record_json: str) -> None:
        if self._raise_on_write:
            raise _FakeBucketError("boom")

    def read_host_record_json(self, host_id: HostId) -> str | None:
        if self._raise_on_read:
            raise _FakeBucketError("boom")
        return self._record_json

    def write_agent_record(self, host_id: HostId, agent_id: str, data: Mapping[str, object]) -> None:
        if self._raise_on_write:
            raise _FakeBucketError("boom")

    def list_agent_records(self, host_id: HostId) -> list[dict]:
        return []

    def remove_agent_record(self, host_id: HostId, agent_id: str) -> None: ...

    def delete_host_state(self, host_id: HostId) -> None: ...

    def host_dir_prefix_has_objects(self, host_id: HostId) -> bool:
        return False

    def volume_for_host(self, host_id: HostId) -> Volume:
        raise NotImplementedError


def _bucket_store(bucket: _FakeBucket) -> BucketHostStateStore:
    return BucketHostStateStore(bucket=bucket, bucket_label="fake bucket")


def test_read_host_record_returns_parsed_bucket_record() -> None:
    """A valid ``host_state.json`` is parsed and returned."""
    host_id = HostId.generate()
    bucket_record = _host_record(host_id, "from-bucket")
    store = _bucket_store(_FakeBucket(record_json=bucket_record.model_dump_json()))
    result = store.read_host_record(host_id)
    assert result is not None
    assert result.certified_host_data.host_name == "from-bucket"


def test_read_host_record_returns_none_when_bucket_record_absent() -> None:
    """No ``host_state.json`` for the host -> a clean None (the host is unknown to the store)."""
    store = _bucket_store(_FakeBucket(record_json=None))
    assert store.read_host_record(HostId.generate()) is None


def test_read_host_record_raises_on_malformed_bucket_record() -> None:
    """A corrupt ``host_state.json`` raises rather than vanishing the host as a clean None."""
    store = _bucket_store(_FakeBucket(record_json="{not valid json"))
    with pytest.raises(MngrError, match="Malformed host record"):
        store.read_host_record(HostId.generate())


def test_read_host_record_propagates_bucket_read_error() -> None:
    """A bucket read failure propagates -- the bucket is required, so a stopped host must not silently vanish."""
    store = _bucket_store(_FakeBucket(raise_on_read=True))
    with pytest.raises(_FakeBucketError):
        store.read_host_record(HostId.generate())


def test_bucket_store_writes_propagate_storage_errors() -> None:
    """Mirror writes propagate a bucket failure (a dropped write would let a stopped host show stale state)."""
    store = _bucket_store(_FakeBucket(raise_on_write=True))
    host_id = HostId.generate()
    with pytest.raises(_FakeBucketError):
        store.persist_host_record(_host_record(host_id, "h"))
    with pytest.raises(_FakeBucketError):
        store.persist_agent_record(host_id, "agent-1", {"id": "agent-1", "name": "a1"})


def test_missing_state_bucket_error_points_at_prepare() -> None:
    """The shared missing-bucket error names the store and its prepare command.

    Providers raise this from ``_state_store`` when the required bucket is absent,
    so create / label / offline reads all fail loudly with an actionable pointer.
    """
    error = missing_state_bucket_error("fake state bucket", "mngr fake prepare")
    assert isinstance(error, MngrError)
    assert "fake state bucket" in str(error)
    assert "mngr fake prepare" in str(error)
