"""Unit tests for ``BlobStateBucket`` against an in-memory Azure Blob fake."""

from imbue.mngr.primitives import HostId
from imbue.mngr_azure.state_bucket import DEFAULT_STATE_CONTAINER_NAME
from imbue.mngr_azure.testing import FakeBlobStorageBackend
from imbue.mngr_azure.testing import _StubbedBlobStateBucket

_ACCOUNT_NAME = "mngrststateacct1234"
_RESOURCE_GROUP = "mngr"
_REGION = "westus"
_SUBSCRIPTION = "sub-1234"


def _make_bucket(backend: FakeBlobStorageBackend) -> _StubbedBlobStateBucket:
    return _StubbedBlobStateBucket(
        credential=None,
        subscription_id=_SUBSCRIPTION,
        resource_group=_RESOURCE_GROUP,
        region=_REGION,
        account_name=_ACCOUNT_NAME,
        fake_backend=backend,
    )


def _make_prepared_bucket() -> tuple[_StubbedBlobStateBucket, FakeBlobStorageBackend]:
    backend = FakeBlobStorageBackend()
    bucket = _make_bucket(backend)
    bucket.ensure_bucket()
    return bucket, backend


def test_ensure_bucket_creates_account_and_container() -> None:
    backend = FakeBlobStorageBackend()
    bucket = _make_bucket(backend)
    assert bucket.account_exists() is False
    assert bucket.container_exists() is False
    assert bucket.ensure_bucket() is True
    assert bucket.account_exists() is True
    assert bucket.container_exists() is True
    assert bucket.container_name == DEFAULT_STATE_CONTAINER_NAME


def test_ensure_bucket_is_idempotent() -> None:
    backend = FakeBlobStorageBackend()
    bucket = _make_bucket(backend)
    assert bucket.ensure_bucket() is True
    # Second call must not re-create the account: returns False (already existed).
    assert bucket.ensure_bucket() is False


def test_ensure_bucket_adds_container_when_account_preexists() -> None:
    # An account that exists without the container (a partial earlier prepare):
    # ensure_bucket returns False (account not created) but still adds the container.
    backend = FakeBlobStorageBackend(account_exists=True)
    bucket = _make_bucket(backend)
    assert bucket.container_exists() is False
    assert bucket.ensure_bucket() is False
    assert bucket.container_exists() is True


def test_host_record_round_trip() -> None:
    bucket, _backend = _make_prepared_bucket()
    host_id = HostId.generate()
    assert bucket.read_host_record(host_id) is None
    record_json = '{"certified_host_data": {"host_id": "x"}}'
    bucket.write_host_record(host_id, record_json)
    assert bucket.read_host_record(host_id) == record_json


def test_agent_records_round_trip_and_remove() -> None:
    bucket, _backend = _make_prepared_bucket()
    host_id = HostId.generate()
    assert bucket.list_agent_records(host_id) == []
    # A labels blob far larger than the 256-char Azure tag limit must survive.
    big_labels = {"k": "v" * 1000}
    bucket.write_agent_record(host_id, "agent-1", {"id": "agent-1", "name": "alpha", "labels": big_labels})
    bucket.write_agent_record(host_id, "agent-2", {"id": "agent-2", "name": "beta"})
    records = bucket.list_agent_records(host_id)
    by_id = {r["id"]: r for r in records}
    assert set(by_id) == {"agent-1", "agent-2"}
    assert by_id["agent-1"]["labels"] == big_labels
    bucket.remove_agent_record(host_id, "agent-1")
    assert {r["id"] for r in bucket.list_agent_records(host_id)} == {"agent-2"}
    # Removing a non-existent record is idempotent.
    bucket.remove_agent_record(host_id, "agent-1")


def test_delete_host_state_removes_record_and_agents() -> None:
    bucket, _backend = _make_prepared_bucket()
    host_id = HostId.generate()
    bucket.write_host_record(host_id, "{}")
    bucket.write_agent_record(host_id, "agent-1", {"id": "agent-1"})
    assert bucket.has_any_host_state() is True
    bucket.delete_host_state(host_id)
    assert bucket.read_host_record(host_id) is None
    assert bucket.list_agent_records(host_id) == []
    assert bucket.has_any_host_state() is False
    # Deleting an already-empty host prefix is idempotent.
    bucket.delete_host_state(host_id)


def test_has_any_host_state_isolated_per_host() -> None:
    bucket, _backend = _make_prepared_bucket()
    assert bucket.has_any_host_state() is False
    host_a = HostId.generate()
    host_b = HostId.generate()
    bucket.write_host_record(host_a, "{}")
    assert bucket.has_any_host_state() is True
    bucket.delete_host_state(host_b)
    # Deleting an unrelated empty host leaves host_a's state intact.
    assert bucket.has_any_host_state() is True


def test_delete_bucket_removes_account_and_state() -> None:
    bucket, backend = _make_prepared_bucket()
    host_id = HostId.generate()
    bucket.write_host_record(host_id, "{}")
    bucket.delete_bucket()
    assert bucket.account_exists() is False
    assert backend.deleted_account is True
    # Deleting an already-absent account is idempotent.
    bucket.delete_bucket()
