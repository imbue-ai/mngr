"""Unit tests for ``GcsStateBucket`` / ``GcsVolume`` using an in-memory fake GCS.

The google-cloud-storage SDK has no first-party in-memory testing harness (no
moto-equivalent), so a small fake covering exactly the methods this module
calls is built here. Test-only subclasses inject the fake via a constructor
field (mirroring ``_StubbedGcpVpsClient``); production code is untouched.
Keeping the fake lean -- and parallel to the production primitives -- makes
the test boundary obvious; richer behavior (versioning, generations, ACLs) is
intentionally absent.
"""

from collections.abc import Iterator
from datetime import datetime
from datetime import timezone
from typing import Any

import pytest
from google.api_core import exceptions as google_api_exceptions
from google.auth.credentials import AnonymousCredentials
from pydantic import Field

from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import HostId
from imbue.mngr_gcp.state_bucket import GcsStateBucket
from imbue.mngr_gcp.state_bucket import GcsStateBucketError
from imbue.mngr_gcp.state_bucket import GcsVolume

# A credential placeholder for the bucket/volume models: pydantic validates the
# field type, but the fake client never actually authenticates with it.
_FAKE_CREDENTIALS = AnonymousCredentials()


class _FakeBlob:
    """A single object in the fake GCS bucket: name + bytes payload + mtime."""

    def __init__(self, parent: "_FakeBucket", name: str, content: bytes) -> None:
        self.parent = parent
        self.name = name
        self.content = content
        self.updated: datetime = datetime.now(timezone.utc)
        self.size: int = len(content)

    def upload_from_string(self, data: bytes | str) -> None:
        content = data.encode("utf-8") if isinstance(data, str) else data
        self.content = content
        self.size = len(content)
        self.updated = datetime.now(timezone.utc)
        self.parent.blobs[self.name] = self

    def download_as_bytes(self) -> bytes:
        existing = self.parent.blobs.get(self.name)
        if existing is None:
            raise google_api_exceptions.NotFound(f"No such object: {self.name}")
        return existing.content

    def delete(self) -> None:
        if self.name not in self.parent.blobs:
            raise google_api_exceptions.NotFound(f"No such object: {self.name}")
        del self.parent.blobs[self.name]

    def exists(self) -> bool:
        return self.name in self.parent.blobs


class _FakeBucket:
    """In-memory GCS bucket: a name -> blob dict plus metadata."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.blobs: dict[str, _FakeBlob] = {}
        self.storage_class: str = "STANDARD"
        self.labels: dict[str, str] = {}
        # The fake's stand-in for ``Bucket.iam_configuration`` -- only the one
        # attribute production touches.
        self.iam_configuration = _FakeIamConfiguration()
        # Set by ``_FakeStorageClient.create_bucket`` (and refreshed by
        # ``bucket()`` on lookup) so ``delete()`` can faithfully remove the
        # bucket from its parent registry. None for detached handles that were
        # never registered, in which case ``delete()`` only clears blobs.
        self.parent_client: "_FakeStorageClient | None" = None

    def blob(self, name: str) -> _FakeBlob:
        existing = self.blobs.get(name)
        if existing is not None:
            return existing
        # Mirror the real SDK: ``bucket.blob(name)`` returns a handle whether or
        # not the object exists. The handle's ``exists()`` / ``delete()`` raise
        # NotFound when the underlying object is absent.
        return _FakeBlob(parent=self, name=name, content=b"")

    def delete(self, force: bool = False) -> None:
        del force
        self.blobs.clear()
        # Mirror real GCS: after ``bucket.delete(...)`` the bucket no longer
        # exists, so a subsequent ``lookup_bucket`` returns None. Without this
        # the fake's ``bucket_exists()`` would keep returning True after a
        # production ``delete_bucket()`` and the idempotency contract could
        # only be verified by external test scaffolding (which would be
        # tautological).
        if self.parent_client is not None:
            self.parent_client.buckets.pop(self.name, None)


class _FakeIamConfiguration:
    """Stand-in for ``Bucket.iam_configuration`` -- production only sets one flag."""

    uniform_bucket_level_access_enabled: bool = False


class _FakeListIterator:
    """A list-iterator that also carries a ``prefixes`` attribute (matches the real SDK)."""

    def __init__(self, blobs: list[_FakeBlob], prefixes: set[str]) -> None:
        self._blobs = blobs
        self.prefixes: set[str] = prefixes

    def __iter__(self) -> Iterator[_FakeBlob]:
        return iter(self._blobs)


class _FakeStorageClient:
    """In-memory GCS client: a bucket-name -> _FakeBucket dict + the methods the bucket calls."""

    def __init__(self) -> None:
        self.buckets: dict[str, _FakeBucket] = {}

    def bucket(self, name: str) -> _FakeBucket:
        # The real SDK returns a handle without creating the bucket -- the bucket
        # itself only materializes via ``create_bucket``. Mirror that: an existing
        # handle is returned if present, else a fresh detached one.
        existing = self.buckets.get(name)
        if existing is not None:
            return existing
        return _FakeBucket(name)

    def lookup_bucket(self, name: str) -> _FakeBucket | None:
        return self.buckets.get(name)

    def get_bucket(self, name: str) -> _FakeBucket:
        existing = self.buckets.get(name)
        if existing is None:
            raise google_api_exceptions.NotFound(f"No such bucket: {name}")
        return existing

    def create_bucket(self, bucket: _FakeBucket, location: str) -> _FakeBucket:
        del location
        if bucket.name in self.buckets:
            raise google_api_exceptions.Conflict(f"Bucket already exists: {bucket.name}")
        # Bind the bucket to this client so ``bucket.delete(...)`` can remove
        # itself from the registry (mirrors the real GCS lifecycle).
        bucket.parent_client = self
        self.buckets[bucket.name] = bucket
        return bucket

    def list_blobs(
        self,
        bucket_or_name: str | _FakeBucket,
        prefix: str = "",
        delimiter: str | None = None,
        max_results: int | None = None,
    ) -> _FakeListIterator:
        bucket_name = bucket_or_name if isinstance(bucket_or_name, str) else bucket_or_name.name
        bucket = self.buckets.get(bucket_name)
        if bucket is None:
            raise google_api_exceptions.NotFound(f"No such bucket: {bucket_name}")
        files: list[_FakeBlob] = []
        prefixes: set[str] = set()
        for blob in bucket.blobs.values():
            if not blob.name.startswith(prefix):
                continue
            if delimiter is None:
                files.append(blob)
                continue
            # Delimited: split the trailing part on the delimiter; if a delimiter
            # appears, classify the immediate-child portion as a sub-"directory".
            tail = blob.name[len(prefix) :]
            if delimiter in tail:
                sub = prefix + tail.split(delimiter, 1)[0] + delimiter
                prefixes.add(sub)
            else:
                files.append(blob)
            if max_results is not None and len(files) >= max_results:
                break
        if max_results is not None:
            files = files[:max_results]
        return _FakeListIterator(blobs=files, prefixes=prefixes)


class _StubbedGcsVolume(GcsVolume):
    """Test-only ``GcsVolume`` that injects a fake storage client via a constructor field.

    Mirrors ``_StubbedGcpVpsClient`` (in ``testing.py``): production
    ``GcsVolume._client()`` builds a real ``storage.Client`` lazily; this subclass
    routes it to the injected fake instead, so the test exercises the
    request-building and response-handling without real GCS calls and without
    monkeypatching the module.
    """

    stubbed_storage_client: Any = Field(default=None, description="Fake storage client")

    def _client(self) -> Any:
        return self.stubbed_storage_client


class _StubbedGcsStateBucket(GcsStateBucket):
    """Test-only ``GcsStateBucket`` that injects a fake storage client + matching volume.

    Overrides ``_make_host_dir_volume`` to produce a ``_StubbedGcsVolume`` bound
    to the same fake, so seeded objects on the bucket are visible to the volume
    reads (the production volume builds its own fresh client otherwise).
    """

    stubbed_storage_client: Any = Field(default=None, description="Fake storage client")

    def _client(self) -> Any:
        return self.stubbed_storage_client

    def _make_host_dir_volume(self) -> Volume:
        return _StubbedGcsVolume(
            credentials=self.credentials,
            project_id=self.project_id,
            bucket_name=self.bucket_name,
            stubbed_storage_client=self.stubbed_storage_client,
        )


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
