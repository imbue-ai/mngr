import json
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

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import HostId

# Object-key layout in the state bucket, per host. The full host record lives
# at ``hosts/<host_id_hex>/host_state.json`` and each agent's record under
# ``hosts/<host_id_hex>/agents/<agent_id>.json``. ``<host_id_hex>`` matches the
# per-host btrfs subvolume naming (``host_id.get_uuid().hex``) so the same id
# keys both the on-instance volume and the bucket.
_HOSTS_PREFIX: Final[str] = "hosts"
_HOST_STATE_FILENAME: Final[str] = "host_state.json"
_AGENTS_SUBPREFIX: Final[str] = "agents"

# ``us-east-1`` is special-cased by S3: CreateBucket rejects a request that
# carries a ``LocationConstraint`` of ``us-east-1`` (the legacy default region
# must be expressed by omitting the constraint entirely). Every other region
# requires the constraint.
_S3_DEFAULT_REGION: Final[str] = "us-east-1"


class S3StateBucketError(MngrError):
    """An S3 state-bucket operation failed."""


class S3StateBucket(MutableModel):
    """Reads/writes mngr control-plane state in an S3 bucket, readable while offline.

    The bucket holds the full host record and per-agent records keyed by host
    id, written by the mngr host machine with the operator's credentials, so a
    stopped instance's state is readable without SSH and without the EC2 tag
    character limit.
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

    def _host_prefix(self, host_id: HostId) -> str:
        return f"{_HOSTS_PREFIX}/{host_id.get_uuid().hex}"

    def _host_state_key(self, host_id: HostId) -> str:
        return f"{self._host_prefix(host_id)}/{_HOST_STATE_FILENAME}"

    def _agent_key(self, host_id: HostId, agent_id: str) -> str:
        return f"{self._host_prefix(host_id)}/{_AGENTS_SUBPREFIX}/{agent_id}.json"

    def write_host_record(self, host_id: HostId, record_json: str) -> None:
        """Write the host record JSON for a host, overwriting any existing object."""
        self._put_object(self._host_state_key(host_id), record_json)

    def read_host_record(self, host_id: HostId) -> str | None:
        """Return the host record JSON for a host, or None if no object exists."""
        return self._get_object(self._host_state_key(host_id))

    def write_agent_record(self, host_id: HostId, agent_id: str, data: Mapping[str, object]) -> None:
        """Write a single agent's record (serialized as JSON) under the host's prefix."""
        self._put_object(self._agent_key(host_id, agent_id), json.dumps(dict(data)))

    def list_agent_records(self, host_id: HostId) -> list[dict]:
        """Return every agent record stored under the host's ``agents/`` prefix.

        A stored object that is not valid JSON (externally edited / corrupted)
        is skipped with a warning rather than crashing the listing.
        """
        agents_prefix = f"{self._host_prefix(host_id)}/{_AGENTS_SUBPREFIX}/"
        records: list[dict] = []
        for key in self._list_keys(agents_prefix):
            body = self._get_object(key)
            if body is None:
                continue
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as e:
                logger.warning("Skipping unparseable agent record {} in bucket {}: {}", key, self.bucket_name, e)
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
            else:
                logger.warning("Skipping agent record {} in bucket {}: not a JSON object", key, self.bucket_name)
        return records

    def remove_agent_record(self, host_id: HostId, agent_id: str) -> None:
        """Delete a single agent's record. Idempotent (no error if absent)."""
        self._delete_object(self._agent_key(host_id, agent_id))

    def delete_host_state(self, host_id: HostId) -> None:
        """Delete every object under the host's prefix. Idempotent."""
        self._delete_keys(self._list_keys(f"{self._host_prefix(host_id)}/"))

    def has_any_host_state(self) -> bool:
        """Return whether any object exists under the ``hosts/`` prefix."""
        with _translate_s3_errors(self.bucket_name):
            response = self._s3().list_objects_v2(Bucket=self.bucket_name, Prefix=f"{_HOSTS_PREFIX}/", MaxKeys=1)
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
                Tagging={"TagSet": [{"Key": "managed-by", "Value": "mngr"}]},
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


@contextmanager
def _translate_s3_errors(bucket_name: str) -> Iterator[None]:
    """Translate ``botocore.ClientError`` into ``S3StateBucketError`` within the block."""
    try:
        yield
    except ClientError as e:
        raise S3StateBucketError(f"S3 operation on bucket {bucket_name!r} failed: {e}") from e
