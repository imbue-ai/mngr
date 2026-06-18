from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import AbstractContextManager
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
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr_vps import state_keys
from imbue.mngr_vps.state_bucket_base import BaseObjectStoreVolume
from imbue.mngr_vps.state_bucket_base import BaseStateBucket
from imbue.mngr_vps.state_bucket_base import ObjectStoreEntry

# S3 not-found error codes: a get/head on a missing object/bucket surfaces one of
# these in the ``ClientError`` response code.
_S3_NOT_FOUND_CODES: Final[frozenset[str]] = frozenset({"NoSuchKey", "404", "NotFound"})

# S3 ``DeleteObjects`` accepts at most this many keys per request.
_S3_DELETE_BATCH_SIZE: Final[int] = 1000

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

    def _translate_errors(self) -> AbstractContextManager[None]:
        return _translate_s3_errors(self.bucket_name)

    def _is_not_found(self, error: MngrError) -> bool:
        return _is_s3_not_found(error)

    @property
    def _bucket_error_type(self) -> type[MngrError]:
        return S3StateBucketError

    def _make_host_dir_volume(self) -> Volume:
        return S3Volume(session=self.session, region=self.region, bucket_name=self.bucket_name)

    def _prefix_has_any_object(self, prefix: str) -> bool:
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

        A ``BucketAlreadyOwnedByYou`` from the create (two concurrent ``prepare``
        runs, or the HeadBucket having raced the create) is treated as an
        idempotent no-op rather than an error: the bucket is ours, so we still
        apply the (idempotent) hardening config and report it as not-created.
        """
        if self.bucket_exists():
            logger.debug("S3 state bucket {} already exists; skipping create", self.bucket_name)
            return False
        create_kwargs: dict[str, Any] = {"Bucket": self.bucket_name}
        # us-east-1 must omit LocationConstraint; every other region requires it.
        if self.region != _S3_DEFAULT_REGION:
            create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
        was_created = True
        try:
            self._s3().create_bucket(**create_kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "BucketAlreadyOwnedByYou":
                raise S3StateBucketError(f"Failed to create S3 bucket {self.bucket_name!r}: {e}") from e
            logger.debug("S3 state bucket {} was created concurrently; applying config idempotently", self.bucket_name)
            was_created = False
        with _translate_s3_errors(self.bucket_name):
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
        if was_created:
            logger.info("Created S3 state bucket {} in region {}", self.bucket_name, self.region)
        return was_created

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

    def _read_object_bytes(self, key: str) -> bytes:
        return self._s3().get_object(Bucket=self.bucket_name, Key=key)["Body"].read()

    def _delete_single_object(self, key: str) -> None:
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
        with _translate_s3_errors(self.bucket_name):
            for start in range(0, len(keys), _S3_DELETE_BATCH_SIZE):
                batch = keys[start : start + _S3_DELETE_BATCH_SIZE]
                response = self._s3().delete_objects(
                    Bucket=self.bucket_name,
                    Delete={"Objects": [{"Key": key} for key in batch]},
                )
                _raise_on_delete_errors(response, self.bucket_name)


class S3Volume(BaseObjectStoreVolume):
    """A ``Volume`` backed by an S3 bucket, for reading a host's offline host_dir.

    Maps volume-relative paths to S3 keys under whatever prefix it is
    ``scoped()`` to. Reads use the operator's credentials (the same session as
    ``S3StateBucket``). The shared object-store logic (listing / existence / read
    / write / delete) lives on ``BaseObjectStoreVolume``; this class supplies only
    the S3 client + SDK primitives + error seam.
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

    def _translate_errors(self) -> AbstractContextManager[None]:
        return _translate_s3_errors(self.bucket_name)

    def _is_not_found(self, error: MngrError) -> bool:
        return _is_s3_not_found(error)

    @property
    def _bucket_error_type(self) -> type[MngrError]:
        return S3StateBucketError

    def _make_missing_file_error(self, path: str) -> MngrError:
        return S3StateBucketError(f"File {path!r} does not exist in bucket {self.bucket_name!r}")

    def _iter_delimited_entries(self, prefix: str) -> Iterator[ObjectStoreEntry]:
        paginator = self._s3().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix, Delimiter="/"):
            # Sub-"directories": each CommonPrefix is one immediate child dir.
            for common in page.get("CommonPrefixes", []):
                yield ObjectStoreEntry(name=common["Prefix"].rstrip("/"), is_directory=True, mtime=0, size=0)
            # Files directly under the prefix.
            for obj in page.get("Contents", []):
                yield ObjectStoreEntry(
                    name=obj["Key"],
                    is_directory=False,
                    mtime=int(obj["LastModified"].timestamp()),
                    size=obj.get("Size", 0),
                )

    def _prefix_has_any_object(self, prefix: str) -> bool:
        response = self._s3().list_objects_v2(Bucket=self.bucket_name, Prefix=prefix, MaxKeys=1)
        return response.get("KeyCount", 0) > 0

    def _has_object_at_key(self, key: str) -> bool:
        response = self._s3().list_objects_v2(Bucket=self.bucket_name, Prefix=key, MaxKeys=1)
        return any(obj["Key"] == key for obj in response.get("Contents", []))

    def _read_object_bytes(self, key: str) -> bytes:
        return self._s3().get_object(Bucket=self.bucket_name, Key=key)["Body"].read()

    def _delete_single_object(self, key: str) -> None:
        self._s3().delete_object(Bucket=self.bucket_name, Key=key)

    def _delete_prefix(self, prefix: str) -> None:
        keys: list[str] = []
        paginator = self._s3().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        for start in range(0, len(keys), _S3_DELETE_BATCH_SIZE):
            batch = keys[start : start + _S3_DELETE_BATCH_SIZE]
            if batch:
                response = self._s3().delete_objects(
                    Bucket=self.bucket_name, Delete={"Objects": [{"Key": key} for key in batch]}
                )
                _raise_on_delete_errors(response, self.bucket_name)

    def _write_object(self, key: str, content: bytes) -> None:
        self._s3().put_object(Bucket=self.bucket_name, Key=key, Body=content)


def _is_s3_not_found(error: MngrError) -> bool:
    """Return whether a translated ``S3StateBucketError`` wraps an S3 not-found ``ClientError``."""
    cause = error.__cause__
    if not isinstance(cause, ClientError):
        return False
    return cause.response.get("Error", {}).get("Code", "") in _S3_NOT_FOUND_CODES


@contextmanager
def _translate_s3_errors(bucket_name: str) -> Iterator[None]:
    """Translate ``botocore.ClientError`` into ``S3StateBucketError`` within the block."""
    try:
        yield
    except ClientError as e:
        raise S3StateBucketError(f"S3 operation on bucket {bucket_name!r} failed: {e}") from e


def _raise_on_delete_errors(response: Mapping[str, Any], bucket_name: str) -> None:
    """Raise if a ``DeleteObjects`` response reports per-key failures.

    S3 ``DeleteObjects`` returns HTTP 200 (no ``ClientError``) even when some keys
    fail to delete; those failures live only in the response's ``Errors`` array. A
    swallowed partial delete would leave orphaned state behind (e.g. a destroyed
    host's records lingering), so surface it instead.
    """
    errors = response.get("Errors", [])
    if errors:
        detail = ", ".join(f"{e.get('Key')!r} ({e.get('Code')}: {e.get('Message')})" for e in errors)
        raise S3StateBucketError(f"S3 failed to delete {len(errors)} object(s) in bucket {bucket_name!r}: {detail}")
