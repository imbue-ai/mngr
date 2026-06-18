"""Unit tests for ``BlobVolume`` (offline host_dir reads) against an in-memory Blob fake."""

import pytest

from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import HostId
from imbue.mngr_azure.state_bucket import BlobStateBucketError
from imbue.mngr_azure.testing import FakeBlobStorageBackend
from imbue.mngr_azure.testing import _StubbedBlobStateBucket
from imbue.mngr_azure.testing import _StubbedBlobVolume

_ACCOUNT = "mngrststateacct1234"
_CONTAINER = "mngr-state"


def _seed(contents: dict[str, bytes]) -> FakeBlobStorageBackend:
    backend = FakeBlobStorageBackend()
    backend.account_exists = True
    backend.container_exists = True
    backend.blobs_by_name = dict(contents)
    return backend


def _volume(backend: FakeBlobStorageBackend, prefix: str) -> Volume:
    return _StubbedBlobVolume(
        credential=None, account_name=_ACCOUNT, container_name=_CONTAINER, fake_backend=backend
    ).scoped(prefix)


def test_read_file_returns_blob_bytes() -> None:
    backend = _seed({"hosts/abc/host_dir/events/messages.jsonl": b"line1\nline2\n"})
    volume = _volume(backend, "hosts/abc/host_dir")
    assert volume.read_file("events/messages.jsonl") == b"line1\nline2\n"


def test_read_file_missing_raises() -> None:
    volume = _volume(_seed({}), "hosts/abc/host_dir")
    with pytest.raises(BlobStateBucketError):
        volume.read_file("nope.txt")


def test_path_exists_for_file_and_directory() -> None:
    backend = _seed({"hosts/abc/host_dir/logs/out.txt": b"x"})
    volume = _volume(backend, "hosts/abc/host_dir")
    assert volume.path_exists("logs/out.txt") is True
    # The "logs" directory exists (a blob shares its prefix).
    assert volume.path_exists("logs") is True
    assert volume.path_exists("missing") is False


def test_listdir_returns_files_and_subdirs() -> None:
    backend = _seed(
        {
            "hosts/abc/host_dir/top.txt": b"hello",
            "hosts/abc/host_dir/logs/a.log": b"a",
            "hosts/abc/host_dir/logs/b.log": b"bb",
        }
    )
    volume = _volume(backend, "hosts/abc/host_dir")
    entries = {e.path: e for e in volume.listdir("")}
    assert set(entries) == {"top.txt", "logs"}
    assert entries["top.txt"].file_type == FileType.FILE
    assert entries["top.txt"].size == len(b"hello")
    assert entries["logs"].file_type == FileType.DIRECTORY
    # Listing the subdirectory yields its files only (relative paths).
    sub = {e.path: e for e in volume.listdir("logs")}
    assert set(sub) == {"a.log", "b.log"}


def test_volume_for_host_scopes_to_host_dir() -> None:
    host_id = HostId.generate()
    hex_id = host_id.get_uuid().hex
    backend = FakeBlobStorageBackend()
    bucket = _StubbedBlobStateBucket(
        credential=None,
        subscription_id="sub-1",
        resource_group="mngr",
        region="westus",
        account_name=_ACCOUNT,
        fake_backend=backend,
    )
    bucket.ensure_bucket()
    backend.blobs_by_name[f"hosts/{hex_id}/host_dir/events/e.jsonl"] = b"evt"
    volume = bucket.volume_for_host(host_id)
    assert volume.read_file("events/e.jsonl") == b"evt"


def test_host_dir_prefix_has_objects() -> None:
    host_id = HostId.generate()
    hex_id = host_id.get_uuid().hex
    backend = FakeBlobStorageBackend()
    bucket = _StubbedBlobStateBucket(
        credential=None,
        subscription_id="sub-1",
        resource_group="mngr",
        region="westus",
        account_name=_ACCOUNT,
        fake_backend=backend,
    )
    bucket.ensure_bucket()
    assert bucket.host_dir_prefix_has_objects(host_id) is False
    backend.blobs_by_name[f"hosts/{hex_id}/host_dir/x"] = b"1"
    assert bucket.host_dir_prefix_has_objects(host_id) is True
