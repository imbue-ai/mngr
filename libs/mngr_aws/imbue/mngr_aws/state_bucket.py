from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any
from typing import Final

import boto3
from botocore.exceptions import ClientError
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.interfaces.volume import BaseVolume
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr_vps_docker import state_keys
from imbue.mngr_vps_docker.state_bucket_base import BaseStateBucket

# ``us-east-1`` is special-cased by S3: CreateBucket rejects a request that
# carries a ``LocationConstraint`` of ``us-east-1`` (the legacy default region
# must be expressed by omitting the constraint entirely). Every other region
# requires the constraint.
_S3_DEFAULT_REGION: Final[str] = "us-east-1"


class S3StateBucketError(MngrError):
    """An S3 state-bucket operation failed."""


class S3StateBucket(BaseStateBucket):
    """Reads/writes mngr control-plane state in an S3 bucket, readable while offline.

    The bucket holds the full host record and per-agent records keyed by host
    id, written by the mngr host machine with the operator's credentials, so a
    stopped instance's state is readable without SSH and without the EC2 tag
    character limit. The cloud-agnostic record marshalling + key layout live on
    ``BaseStateBucket``; this class supplies the S3 client, the raw object
    primitives, error translation, and the bucket lifecycle.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session: boto3.Session = Field(frozen=True, description="boto3 Session with resolved credentials")
    region: str = Field(frozen=True, description="AWS region the bucket lives in")
    bucket_name: str = Field(frozen=True, description="Name of the S3 bucket holding mngr state")
    _cached_s3_client: Any = PrivateAttr(default=None)

    def _s3(self) -> Any:
        """Return the S3 client, building and caching it from the session on first use."""
        if self._cached_s3_client is None:
            self._cached_s3_client = self.session.client("s3", region_name=self.region)
        return self._cached_s3_client

    @property
    def _store_label(self) -> str:
        return f"S3 bucket {self.bucket_name}"

    def _make_host_dir_volume(self) -> Volume:
        return S3Volume(session=self.session, region=self.region, bucket_name=self.bucket_name)

    def _prefix_has_objects(self, prefix: str) -> bool:
        with _translate_s3_errors(self.bucket_name):
            response = self._s3().list_objects_v2(Bucket=self.bucket_name, Prefix=prefix, MaxKeys=1)
        return response.get("KeyCount", 0) > 0

    def bucket_exists(self) -> bool:
        """Return whether the bucket already exists (read-only HeadBucket)."""
        try:
            self._s3().head_bucket(Bucket=self.bucket_name)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchBucket", "NotFound"):
                return False
            raise S3StateBucketError(f"Failed to check existence of S3 bucket {self.bucket_name!r}: {e}") from e
        return True

    def ensure_bucket(self) -> bool:
        """Idempotently create the state bucket, returning True iff it was created.

        Read-only-first: a HeadBucket precedes any create, so a re-run on an
        already-prepared bucket issues no write. The created bucket is private
        (public access blocked), encrypted at rest with SSE-S3, and tagged
        ``managed-by=mngr``.
        """
        if self.bucket_exists():
            logger.debug("S3 state bucket {} already exists; skipping create", self.bucket_name)
            return False
        create_kwargs: dict[str, Any] = {"Bucket": self.bucket_name}
        # us-east-1 must omit LocationConstraint; every other region requires it.
        if self.region != _S3_DEFAULT_REGION:
            create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
        with _translate_s3_errors(self.bucket_name):
            self._s3().create_bucket(**create_kwargs)
            self._s3().put_public_access_block(
                Bucket=self.bucket_name,
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                },
            )
            self._s3().put_bucket_encryption(
                Bucket=self.bucket_name,
                ServerSideEncryptionConfiguration={
                    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
                },
            )
            self._s3().put_bucket_tagging(
                Bucket=self.bucket_name,
                Tagging={"TagSet": [{"Key": state_keys.MANAGED_BY_TAG_KEY, "Value": state_keys.MANAGED_BY_TAG_VALUE}]},
            )
        logger.info("Created S3 state bucket {} in region {}", self.bucket_name, self.region)
        return True

    def delete_bucket(self) -> None:
        """Empty and delete the bucket. Idempotent (no error if already absent)."""
        if not self.bucket_exists():
            return
        self._delete_keys(self._list_keys(""))
        with _translate_s3_errors(self.bucket_name):
            self._s3().delete_bucket(Bucket=self.bucket_name)
        logger.info("Deleted S3 state bucket {} in region {}", self.bucket_name, self.region)

    def _put_object(self, key: str, body: str) -> None:
        with _translate_s3_errors(self.bucket_name):
            self._s3().put_object(Bucket=self.bucket_name, Key=key, Body=body.encode("utf-8"))

    def _get_object(self, key: str) -> str | None:
        try:
            response = self._s3().get_object(Bucket=self.bucket_name, Key=key)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise S3StateBucketError(f"Failed to read object {key!r} from bucket {self.bucket_name!r}: {e}") from e
        return response["Body"].read().decode("utf-8")

    def _delete_object(self, key: str) -> None:
        with _translate_s3_errors(self.bucket_name):
            self._s3().delete_object(Bucket=self.bucket_name, Key=key)

    def _list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        with _translate_s3_errors(self.bucket_name):
            paginator = self._s3().get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"])
        return keys

    def _delete_keys(self, keys: list[str]) -> None:
        if not keys:
            return
        # S3 DeleteObjects accepts at most 1000 keys per request.
        with _translate_s3_errors(self.bucket_name):
            for start in range(0, len(keys), 1000):
                batch = keys[start : start + 1000]
                self._s3().delete_objects(
                    Bucket=self.bucket_name,
                    Delete={"Objects": [{"Key": key} for key in batch]},
                )


class S3Volume(BaseVolume):
    """A ``Volume`` backed by an S3 bucket, for reading a host's offline host_dir.

    Maps volume-relative paths to S3 keys under whatever prefix it is
    ``scoped()`` to. Reads use the operator's credentials (the same session as
    ``S3StateBucket``). S3 has no real directories, so a "directory" is the set
    of keys sharing a prefix; ``listdir`` synthesizes directory entries from the
    common prefixes a delimited list returns.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session: boto3.Session = Field(frozen=True, description="boto3 Session with resolved credentials")
    region: str = Field(frozen=True, description="AWS region the bucket lives in")
    bucket_name: str = Field(frozen=True, description="Name of the S3 bucket")
    _cached_s3_client: Any = PrivateAttr(default=None)

    def _s3(self) -> Any:
        if self._cached_s3_client is None:
            self._cached_s3_client = self.session.client("s3", region_name=self.region)
        return self._cached_s3_client

    def listdir(self, path: str) -> list[VolumeFile]:
        prefix = _as_dir_prefix(path)
        entries: list[VolumeFile] = []
        with _translate_s3_errors(self.bucket_name):
            paginator = self._s3().get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix, Delimiter="/"):
                # Sub-"directories": each CommonPrefix is one immediate child dir.
                for common in page.get("CommonPrefixes", []):
                    child = common["Prefix"][len(prefix) :].rstrip("/")
                    if child:
                        entries.append(VolumeFile(path=child, file_type=FileType.DIRECTORY, mtime=0, size=0))
                # Files directly under the prefix (skip the prefix placeholder key itself).
                for obj in page.get("Contents", []):
                    child = obj["Key"][len(prefix) :]
                    if not child or "/" in child:
                        continue
                    entries.append(
                        VolumeFile(
                            path=child,
                            file_type=FileType.FILE,
                            mtime=int(obj["LastModified"].timestamp()),
                            size=obj.get("Size", 0),
                        )
                    )
        return entries

    def path_exists(self, path: str) -> bool:
        key = path.lstrip("/")
        # Two probes, because a single list on the bare prefix can return a
        # lexicographically-earlier sibling (e.g. ``foobar`` when probing dir
        # ``foo``) that matches neither test. The directory probe lists the
        # ``foo/`` prefix; the exact-file probe lists the bare key and checks
        # for an exact match. Mirrors the Azure ``BlobVolume.path_exists``.
        dir_prefix = _as_dir_prefix(path)
        with _translate_s3_errors(self.bucket_name):
            if dir_prefix:
                directory = self._s3().list_objects_v2(Bucket=self.bucket_name, Prefix=dir_prefix, MaxKeys=1)
                if directory.get("KeyCount", 0) > 0:
                    return True
            exact = self._s3().list_objects_v2(Bucket=self.bucket_name, Prefix=key, MaxKeys=1)
        return any(obj["Key"] == key for obj in exact.get("Contents", []))

    def read_file(self, path: str) -> bytes:
        key = path.lstrip("/")
        try:
            response = self._s3().get_object(Bucket=self.bucket_name, Key=key)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NotFound"):
                raise S3StateBucketError(f"File {path!r} does not exist in bucket {self.bucket_name!r}") from e
            raise S3StateBucketError(f"Failed to read {path!r} from bucket {self.bucket_name!r}: {e}") from e
        return response["Body"].read()

    def remove_file(self, path: str, *, recursive: bool = False) -> None:
        if recursive:
            self.remove_directory(path)
            return
        with _translate_s3_errors(self.bucket_name):
            self._s3().delete_object(Bucket=self.bucket_name, Key=path.lstrip("/"))

    def remove_directory(self, path: str) -> None:
        prefix = _as_dir_prefix(path)
        keys: list[str] = []
        with _translate_s3_errors(self.bucket_name):
            paginator = self._s3().get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                keys.extend(obj["Key"] for obj in page.get("Contents", []))
            for start in range(0, len(keys), 1000):
                batch = keys[start : start + 1000]
                if batch:
                    self._s3().delete_objects(
                        Bucket=self.bucket_name, Delete={"Objects": [{"Key": key} for key in batch]}
                    )

    def write_files(self, file_contents_by_path: Mapping[str, bytes]) -> None:
        with _translate_s3_errors(self.bucket_name):
            for path, content in file_contents_by_path.items():
                self._s3().put_object(Bucket=self.bucket_name, Key=path.lstrip("/"), Body=content)


def _as_dir_prefix(path: str) -> str:
    """Normalize a volume path to an S3 directory prefix (no leading slash, trailing slash)."""
    cleaned = path.strip("/")
    return f"{cleaned}/" if cleaned else ""


@contextmanager
def _translate_s3_errors(bucket_name: str) -> Iterator[None]:
    """Translate ``botocore.ClientError`` into ``S3StateBucketError`` within the block."""
    try:
        yield
    except ClientError as e:
        raise S3StateBucketError(f"S3 operation on bucket {bucket_name!r} failed: {e}") from e
