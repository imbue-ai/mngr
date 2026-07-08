"""Unit tests for ``S3Volume`` (offline host_dir reads) using moto's in-memory S3."""

from collections.abc import Iterator

import boto3
import pytest

from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.primitives import HostId
from imbue.mngr_aws.state_bucket import S3StateBucket
from imbue.mngr_aws.state_bucket import S3StateBucketError
from imbue.mngr_aws.state_bucket import S3Volume

_US_EAST_1 = "us-east-1"
_BUCKET = "mngr-state-volume-tests"


@pytest.fixture
def seeded_aws_session(aws_session: boto3.Session) -> Iterator[boto3.Session]:
    """The shared moto session with the volume tests' ``_BUCKET`` already created.

    Builds on the shared ``aws_session`` fixture (conftest) rather than re-opening
    moto, adding only the bucket-creation these volume tests need.
    """
    S3StateBucket(session=aws_session, region=_US_EAST_1, bucket_name=_BUCKET).ensure_bucket()
    yield aws_session


def _raw_volume(session: boto3.Session) -> S3Volume:
    return S3Volume(session=session, region=_US_EAST_1, bucket_name=_BUCKET)


def _seed(session: boto3.Session, key_contents: dict[str, bytes]) -> None:
    s3 = session.client("s3", region_name=_US_EAST_1)
    for key, content in key_contents.items():
        s3.put_object(Bucket=_BUCKET, Key=key, Body=content)


def test_read_file_returns_object_bytes(seeded_aws_session: boto3.Session) -> None:
    _seed(seeded_aws_session, {"hosts/abc/host_dir/events/messages.jsonl": b"line1\nline2\n"})
    volume = _raw_volume(seeded_aws_session).scoped("hosts/abc/host_dir")
    assert volume.read_file("events/messages.jsonl") == b"line1\nline2\n"


def test_read_file_missing_raises(seeded_aws_session: boto3.Session) -> None:
    volume = _raw_volume(seeded_aws_session).scoped("hosts/abc/host_dir")
    with pytest.raises(S3StateBucketError):
        volume.read_file("nope.txt")


def test_path_exists_for_file_and_directory(seeded_aws_session: boto3.Session) -> None:
    _seed(seeded_aws_session, {"hosts/abc/host_dir/logs/out.txt": b"x"})
    volume = _raw_volume(seeded_aws_session).scoped("hosts/abc/host_dir")
    assert volume.path_exists("logs/out.txt") is True
    # The "logs" directory exists (a key shares its prefix).
    assert volume.path_exists("logs") is True
    assert volume.path_exists("missing") is False


def test_path_exists_with_prefix_colliding_sibling(seeded_aws_session: boto3.Session) -> None:
    # A file ``foobar`` shares the bare prefix of dir ``foo`` and sorts earlier,
    # so a single MaxKeys=1 list on ``foo`` would return ``foobar`` and wrongly
    # report the directory absent. The directory probe lists ``foo/`` instead.
    _seed(
        seeded_aws_session,
        {
            "hosts/abc/host_dir/foobar": b"f",
            "hosts/abc/host_dir/foo/bar": b"b",
        },
    )
    volume = _raw_volume(seeded_aws_session).scoped("hosts/abc/host_dir")
    assert volume.path_exists("foo") is True
    assert volume.path_exists("foobar") is True
    assert volume.path_exists("nope") is False


def test_listdir_returns_files_and_subdirs(seeded_aws_session: boto3.Session) -> None:
    _seed(
        seeded_aws_session,
        {
            "hosts/abc/host_dir/top.txt": b"hello",
            "hosts/abc/host_dir/logs/a.log": b"a",
            "hosts/abc/host_dir/logs/b.log": b"bb",
        },
    )
    volume = _raw_volume(seeded_aws_session).scoped("hosts/abc/host_dir")
    entries = {e.path: e for e in volume.listdir("")}
    assert set(entries) == {"top.txt", "logs"}
    assert entries["top.txt"].file_type == FileType.FILE
    assert entries["top.txt"].size == len(b"hello")
    assert entries["top.txt"].mtime > 0
    assert entries["logs"].file_type == FileType.DIRECTORY
    # Listing the subdirectory yields its files only (relative paths).
    sub = {e.path: e for e in volume.listdir("logs")}
    assert set(sub) == {"a.log", "b.log"}


def test_volume_for_host_scopes_to_host_dir(seeded_aws_session: boto3.Session) -> None:
    host_id = HostId.generate()
    hex_id = host_id.get_uuid().hex
    _seed(seeded_aws_session, {f"hosts/{hex_id}/host_dir/events/e.jsonl": b"evt"})
    bucket = S3StateBucket(session=seeded_aws_session, region=_US_EAST_1, bucket_name=_BUCKET)
    volume = bucket.volume_for_host(host_id)
    assert volume.read_file("events/e.jsonl") == b"evt"


def test_host_dir_prefix_has_objects(seeded_aws_session: boto3.Session) -> None:
    host_id = HostId.generate()
    hex_id = host_id.get_uuid().hex
    bucket = S3StateBucket(session=seeded_aws_session, region=_US_EAST_1, bucket_name=_BUCKET)
    assert bucket.host_dir_prefix_has_objects(host_id) is False
    _seed(seeded_aws_session, {f"hosts/{hex_id}/host_dir/x": b"1"})
    assert bucket.host_dir_prefix_has_objects(host_id) is True


def test_write_and_remove_round_trip(seeded_aws_session: boto3.Session) -> None:
    volume = _raw_volume(seeded_aws_session).scoped("hosts/abc/host_dir")
    volume.write_files({"dir/f.txt": b"data"})
    assert volume.read_file("dir/f.txt") == b"data"
    volume.remove_file("dir/f.txt")
    assert volume.path_exists("dir/f.txt") is False
