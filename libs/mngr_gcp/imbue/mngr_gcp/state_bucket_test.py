"""Unit tests for ``GcsStateBucket`` / ``GcsVolume`` using an in-memory fake GCS.

The in-memory fake GCS (``_FakeStorageClient`` and friends) and the
``_Stubbed*`` injection seams live in ``testing.py`` so multiple test modules
can import them uniformly (matches ``mngr_azure``'s placement of
``_StubbedBlobStateBucket``).
"""

from typing import Any

import pytest
from google.api_core import exceptions as google_api_exceptions

from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.primitives import HostId
from imbue.mngr_gcp.state_bucket import GcsStateBucket
from imbue.mngr_gcp.state_bucket import GcsStateBucketError
from imbue.mngr_gcp.state_bucket import GcsVolume
from imbue.mngr_gcp.testing import _FAKE_CREDENTIALS
from imbue.mngr_gcp.testing import _FakeBucket
from imbue.mngr_gcp.testing import _FakeStorageClient
from imbue.mngr_gcp.testing import _StubbedGcsStateBucket
from imbue.mngr_gcp.testing import _StubbedGcsVolume


@pytest.fixture
def fake_gcs() -> _FakeStorageClient:
    """A fresh fake GCS client for each test (function-scoped)."""
    return _FakeStorageClient()


def _make_bucket(fake: _FakeStorageClient, bucket_name: str = "mngr-state-test") -> GcsStateBucket:
    return _StubbedGcsStateBucket(
        credentials=_FAKE_CREDENTIALS,
        project_id="test-project",
        region="us-west1",
        bucket_name=bucket_name,
        stubbed_storage_client=fake,
    )


def test_ensure_bucket_creates_when_absent(fake_gcs: _FakeStorageClient) -> None:
    bucket = _make_bucket(fake_gcs)
    assert bucket.bucket_exists() is False
    assert bucket.ensure_bucket() is True
    assert bucket.bucket_exists() is True


def test_ensure_bucket_is_idempotent(fake_gcs: _FakeStorageClient) -> None:
    bucket = _make_bucket(fake_gcs)
    assert bucket.ensure_bucket() is True
    # Second call must not re-create: returns False (already existed).
    assert bucket.ensure_bucket() is False


def test_host_record_round_trip(fake_gcs: _FakeStorageClient) -> None:
    bucket = _make_bucket(fake_gcs)
    bucket.ensure_bucket()
    host_id = HostId.generate()
    assert bucket.read_host_record_json(host_id) is None
    record_json = '{"certified_host_data": {"host_id": "x"}}'
    bucket.write_host_record_json(host_id, record_json)
    assert bucket.read_host_record_json(host_id) == record_json


def test_agent_records_round_trip_and_remove(fake_gcs: _FakeStorageClient) -> None:
    bucket = _make_bucket(fake_gcs)
    bucket.ensure_bucket()
    host_id = HostId.generate()
    assert bucket.list_agent_records(host_id) == []
    big_labels = {"k": "v" * 1000}
    bucket.write_agent_record(host_id, "agent-1", {"id": "agent-1", "name": "alpha", "labels": big_labels})
    bucket.write_agent_record(host_id, "agent-2", {"id": "agent-2", "name": "beta"})
    records = bucket.list_agent_records(host_id)
    by_id = {r["id"]: r for r in records}
    assert set(by_id) == {"agent-1", "agent-2"}
    # The >256-char labels blob (which a GCE-label mirror would drop) survives in the bucket.
    assert by_id["agent-1"]["labels"] == big_labels
    bucket.remove_agent_record(host_id, "agent-1")
    assert {r["id"] for r in bucket.list_agent_records(host_id)} == {"agent-2"}
    # Removing a non-existent record is idempotent.
    bucket.remove_agent_record(host_id, "agent-1")


def test_delete_host_state_removes_record_and_agents(fake_gcs: _FakeStorageClient) -> None:
    bucket = _make_bucket(fake_gcs)
    bucket.ensure_bucket()
    host_id = HostId.generate()
    bucket.write_host_record_json(host_id, "{}")
    bucket.write_agent_record(host_id, "agent-1", {"id": "agent-1"})
    assert bucket.has_any_host_state() is True
    bucket.delete_host_state(host_id)
    assert bucket.read_host_record_json(host_id) is None
    assert bucket.list_agent_records(host_id) == []
    assert bucket.has_any_host_state() is False
    # Deleting an already-empty host prefix is idempotent.
    bucket.delete_host_state(host_id)


def test_has_any_host_state_isolated_per_host(fake_gcs: _FakeStorageClient) -> None:
    bucket = _make_bucket(fake_gcs)
    bucket.ensure_bucket()
    assert bucket.has_any_host_state() is False
    host_a = HostId.generate()
    host_b = HostId.generate()
    bucket.write_host_record_json(host_a, "{}")
    assert bucket.has_any_host_state() is True
    bucket.delete_host_state(host_b)
    assert bucket.has_any_host_state() is True


def test_delete_bucket_is_idempotent(fake_gcs: _FakeStorageClient) -> None:
    bucket = _make_bucket(fake_gcs)
    bucket.ensure_bucket()
    bucket.delete_bucket()
    # Mirrors real GCS: the first delete removes the bucket; the second is a
    # no-op short-circuited by the existence probe in ``delete_bucket``.
    assert bucket.bucket_exists() is False
    bucket.delete_bucket()


def test_volume_for_host_serves_files_from_host_dir_prefix(fake_gcs: _FakeStorageClient) -> None:
    """The bucket-derived volume reads the host's host_dir/ tree."""
    bucket = _make_bucket(fake_gcs)
    bucket.ensure_bucket()
    host_id = HostId.generate()
    hex_id = host_id.get_uuid().hex
    # Seed an event file under the host's host_dir prefix.
    fake_gcs.buckets[bucket.bucket_name].blob(f"hosts/{hex_id}/host_dir/events/e.jsonl").upload_from_string(b"evt")
    volume = bucket.volume_for_host(host_id)
    assert volume.read_file("events/e.jsonl") == b"evt"


def test_host_dir_prefix_has_objects_signals_capture(fake_gcs: _FakeStorageClient) -> None:
    """`host_dir_prefix_has_objects` returns True only after something was uploaded under it."""
    bucket = _make_bucket(fake_gcs)
    bucket.ensure_bucket()
    host_id = HostId.generate()
    assert bucket.host_dir_prefix_has_objects(host_id) is False
    hex_id = host_id.get_uuid().hex
    fake_gcs.buckets[bucket.bucket_name].blob(f"hosts/{hex_id}/host_dir/marker.txt").upload_from_string(b"hi")
    assert bucket.host_dir_prefix_has_objects(host_id) is True


def _make_volume(fake_gcs: _FakeStorageClient, bucket_name: str = "mngr-state-test") -> GcsVolume:
    return _StubbedGcsVolume(
        credentials=_FAKE_CREDENTIALS,
        project_id="test-project",
        bucket_name=bucket_name,
        stubbed_storage_client=fake_gcs,
    )


def test_volume_listdir_synthesizes_subdirectory_entries(fake_gcs: _FakeStorageClient) -> None:
    fake_gcs.buckets["mngr-state-test"] = _FakeBucket("mngr-state-test")
    bucket = fake_gcs.buckets["mngr-state-test"]
    bucket.blob("a/file1.txt").upload_from_string(b"1")
    bucket.blob("a/file2.txt").upload_from_string(b"2")
    bucket.blob("a/sub/file3.txt").upload_from_string(b"3")
    volume = _make_volume(fake_gcs)
    entries = volume.listdir("/a")
    by_name = {e.path: e for e in entries}
    assert by_name["file1.txt"].file_type == FileType.FILE
    assert by_name["file2.txt"].file_type == FileType.FILE
    assert by_name["sub"].file_type == FileType.DIRECTORY


def test_volume_read_file_raises_when_missing(fake_gcs: _FakeStorageClient) -> None:
    fake_gcs.buckets["mngr-state-test"] = _FakeBucket("mngr-state-test")
    volume = _make_volume(fake_gcs)
    with pytest.raises(GcsStateBucketError) as exc_info:
        volume.read_file("/nope.txt")
    assert "nope.txt" in str(exc_info.value)


def test_volume_write_files_uploads_each(fake_gcs: _FakeStorageClient) -> None:
    fake_gcs.buckets["mngr-state-test"] = _FakeBucket("mngr-state-test")
    volume = _make_volume(fake_gcs)
    volume.write_files({"a/b.txt": b"hello", "c.txt": b"world"})
    bucket = fake_gcs.buckets["mngr-state-test"]
    assert bucket.blobs["a/b.txt"].content == b"hello"
    assert bucket.blobs["c.txt"].content == b"world"


def test_volume_remove_file_idempotent(fake_gcs: _FakeStorageClient) -> None:
    fake_gcs.buckets["mngr-state-test"] = _FakeBucket("mngr-state-test")
    bucket = fake_gcs.buckets["mngr-state-test"]
    bucket.blob("doomed.txt").upload_from_string(b"x")
    volume = _make_volume(fake_gcs)
    volume.remove_file("doomed.txt")
    assert "doomed.txt" not in bucket.blobs
    # Removing again is a no-op (not an error).
    volume.remove_file("doomed.txt")


class _FlakyLookupClient(_FakeStorageClient):
    """A fake that always raises an InternalServerError on ``lookup_bucket``.

    Used to exercise the ``bucket_exists`` error-translation path: a storage
    error during the existence probe must surface as ``GcsStateBucketError``
    rather than masquerading as "absent" (returning False would let callers
    quietly treat a 5xx as "no bucket" and refuse offline reads).
    """

    def lookup_bucket(self, name: str) -> Any:
        del name
        raise google_api_exceptions.InternalServerError("flaky GCS")


def test_bucket_exists_translates_storage_errors() -> None:
    """A storage error during the existence probe surfaces as ``GcsStateBucketError`` (not 'absent')."""
    bucket = _make_bucket(_FlakyLookupClient())
    with pytest.raises(GcsStateBucketError):
        bucket.bucket_exists()
