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
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.interfaces.volume import BaseVolume
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_vps_docker import state_keys

# Trust policy + inline policy name for the per-bucket host identity (Decision 3).
# The trust policy lets EC2 assume the role; the inline policy grants ONLY the
# object actions the on-box sync daemon needs (Put/Get/Delete) scoped to this
# bucket's ``hosts/*`` prefix, plus ``s3:ListBucket`` on the bucket (required by
# ``aws s3 sync --delete`` to enumerate the destination) -- least privilege: the
# operator's credentials, not this role, write host/agent records.
_HOST_IDENTITY_INLINE_POLICY_NAME: Final[str] = "mngr-host-dir-sync"
_EC2_TRUST_POLICY: Final[str] = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
)

# ``us-east-1`` is special-cased by S3: CreateBucket rejects a request that
# carries a ``LocationConstraint`` of ``us-east-1`` (the legacy default region
# must be expressed by omitting the constraint entirely). Every other region
# requires the constraint.
_S3_DEFAULT_REGION: Final[str] = "us-east-1"


class S3StateBucketError(MngrError):
    """An S3 state-bucket operation failed."""


class S3StateHostIdentityError(MngrError):
    """An IAM host-identity (role / instance-profile) operation failed."""


def host_identity_name_for_bucket(bucket_name: str) -> str:
    """Return the deterministic IAM role / instance-profile name for a state bucket.

    The bucket name already encodes the account id + region (or an operator
    override), so deriving the identity name from it gives one stable identity
    per ``prepare`` scope. IAM names allow ``[\\w+=,.@-]`` up to 128 chars; the
    bucket name is DNS-form (lowercase alphanumerics + dashes), so it is a valid
    suffix as-is.
    """
    return f"mngr-aws-host-{bucket_name}"


def host_dir_sync_target_for(bucket_name: str, host_id: HostId) -> str:
    """Return the ``s3://<bucket>/hosts/<host_id_hex>/host_dir/`` sync destination URI."""
    return f"s3://{bucket_name}/{state_keys.host_dir_prefix(host_id)}"


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

    def write_host_record(self, host_id: HostId, record_json: str) -> None:
        """Write the host record JSON for a host, overwriting any existing object."""
        self._put_object(state_keys.host_state_key(host_id), record_json)

    def read_host_record(self, host_id: HostId) -> str | None:
        """Return the host record JSON for a host, or None if no object exists."""
        return self._get_object(state_keys.host_state_key(host_id))

    def write_agent_record(self, host_id: HostId, agent_id: str, data: Mapping[str, object]) -> None:
        """Write a single agent's record (serialized as JSON) under the host's prefix."""
        self._put_object(state_keys.agent_key(host_id, agent_id), json.dumps(dict(data)))

    def list_agent_records(self, host_id: HostId) -> list[dict]:
        """Return every agent record stored under the host's ``agents/`` prefix.

        A stored object that is not valid JSON (externally edited / corrupted)
        is skipped with a warning rather than crashing the listing.
        """
        records: list[dict] = []
        for key in self._list_keys(state_keys.agents_prefix(host_id)):
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
        self._delete_object(state_keys.agent_key(host_id, agent_id))

    def delete_host_state(self, host_id: HostId) -> None:
        """Delete every object under the host's prefix. Idempotent."""
        self._delete_keys(self._list_keys(f"{state_keys.host_prefix(host_id)}/"))

    def volume_for_host(self, host_id: HostId) -> Volume:
        """Return a Volume scoped to ``hosts/<host_id_hex>/host_dir/`` for offline reads.

        Reads use the operator's credentials (this same session), so no instance
        identity is required to read -- only to push. The returned volume is
        rooted at the host's ``host_dir`` tree, matching how
        ``OfflineHostWithVolume`` addresses files (relative to ``host_dir``).
        """
        host_dir_prefix = state_keys.host_dir_prefix(host_id).rstrip("/")
        return S3Volume(session=self.session, region=self.region, bucket_name=self.bucket_name).scoped(host_dir_prefix)

    def host_dir_prefix_has_objects(self, host_id: HostId) -> bool:
        """Return whether any object exists under the host's ``host_dir/`` prefix.

        Used by the offline-read path as a light existence probe: an empty prefix
        means the instance never pushed its host_dir (e.g. the sync daemon never
        ran, or the instance has no bucket-write identity).
        """
        prefix = state_keys.host_dir_prefix(host_id)
        with _translate_s3_errors(self.bucket_name):
            response = self._s3().list_objects_v2(Bucket=self.bucket_name, Prefix=prefix, MaxKeys=1)
        return response.get("KeyCount", 0) > 0

    def has_any_host_state(self) -> bool:
        """Return whether any object exists under the ``hosts/`` prefix."""
        with _translate_s3_errors(self.bucket_name):
            response = self._s3().list_objects_v2(
                Bucket=self.bucket_name, Prefix=f"{state_keys.HOSTS_PREFIX}/", MaxKeys=1
            )
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


def build_host_identity_inline_policy(bucket_name: str) -> str:
    """Build the least-privilege inline policy JSON for the bucket-write host identity.

    Grants ONLY the object actions the on-box sync daemon needs --
    ``s3:PutObject`` / ``s3:GetObject`` / ``s3:DeleteObject`` on the
    ``hosts/*`` prefix, and ``s3:ListBucket`` on the bucket itself (so
    ``aws s3 sync --delete`` can enumerate the destination). Nothing else: the
    role cannot read/write outside this bucket, nor any object outside
    ``hosts/*``.
    """
    bucket_arn = f"arn:aws:s3:::{bucket_name}"
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "ListStateBucket",
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket"],
                    "Resource": [bucket_arn],
                },
                {
                    "Sid": "ReadWriteHostObjects",
                    "Effect": "Allow",
                    "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
                    "Resource": [f"{bucket_arn}/{state_keys.HOSTS_PREFIX}/*"],
                },
            ],
        }
    )


class S3StateHostIdentity(MutableModel):
    """Manages the IAM role + instance profile that lets an EC2 instance push host_dir to the bucket.

    The role is assumable by EC2 and carries a least-privilege inline policy
    scoped to the state bucket's ``hosts/*`` prefix. Provisioned by
    ``mngr aws prepare`` (Decision 3) and attached at host create so the on-box
    sync daemon can write via IMDS credentials. Reads never need this identity --
    they use the operator's credentials.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session: boto3.Session = Field(frozen=True, description="boto3 Session with resolved credentials")
    region: str = Field(frozen=True, description="AWS region (for the IAM client)")
    bucket_name: str = Field(frozen=True, description="State bucket the identity is scoped to")
    _cached_iam_client: Any = PrivateAttr(default=None)

    def _iam(self) -> Any:
        """Return the IAM client, building and caching it from the session on first use."""
        if self._cached_iam_client is None:
            self._cached_iam_client = self.session.client("iam", region_name=self.region)
        return self._cached_iam_client

    @property
    def identity_name(self) -> str:
        """The deterministic role / instance-profile name for this bucket."""
        return host_identity_name_for_bucket(self.bucket_name)

    def host_identity_exists(self) -> bool:
        """Return whether the instance profile already exists (read-only GetInstanceProfile)."""
        try:
            self._iam().get_instance_profile(InstanceProfileName=self.identity_name)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchEntity", "404"):
                return False
            raise S3StateHostIdentityError(f"Failed to check IAM instance profile {self.identity_name!r}: {e}") from e
        return True

    def ensure_host_identity(self) -> str:
        """Idempotently create the role + inline policy + instance profile, returning the profile name.

        Read-only-first: a ``GetInstanceProfile`` precedes any create, so a
        re-run on an already-provisioned identity issues no write. Otherwise it
        creates the EC2-assumable role, attaches the least-privilege inline
        policy, creates the instance profile, and adds the role to it.
        """
        if self.host_identity_exists():
            logger.debug("IAM host identity {} already exists; skipping create", self.identity_name)
            return self.identity_name
        with _translate_iam_errors(self.identity_name):
            self._ensure_role()
            self._iam().put_role_policy(
                RoleName=self.identity_name,
                PolicyName=_HOST_IDENTITY_INLINE_POLICY_NAME,
                PolicyDocument=build_host_identity_inline_policy(self.bucket_name),
            )
            self._ensure_instance_profile_with_role()
        logger.info("Provisioned IAM host identity {} for state bucket {}", self.identity_name, self.bucket_name)
        return self.identity_name

    def delete_host_identity(self) -> None:
        """Tear down the instance profile + role. Idempotent (no error if already absent)."""
        with _translate_iam_errors(self.identity_name):
            self._delete_instance_profile_if_present()
            self._delete_role_if_present()
        logger.info("Deleted IAM host identity {} for state bucket {}", self.identity_name, self.bucket_name)

    def _ensure_role(self) -> None:
        try:
            self._iam().create_role(
                RoleName=self.identity_name,
                AssumeRolePolicyDocument=_EC2_TRUST_POLICY,
                Description=f"Auto-created by mngr_aws so EC2 instances can sync host_dir to {self.bucket_name}",
                Tags=[{"Key": "managed-by", "Value": "mngr"}],
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code", "") != "EntityAlreadyExists":
                raise

    def _ensure_instance_profile_with_role(self) -> None:
        try:
            self._iam().create_instance_profile(
                InstanceProfileName=self.identity_name,
                Tags=[{"Key": "managed-by", "Value": "mngr"}],
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code", "") != "EntityAlreadyExists":
                raise
        try:
            self._iam().add_role_to_instance_profile(
                InstanceProfileName=self.identity_name,
                RoleName=self.identity_name,
            )
        except ClientError as e:
            # The role is already attached: IAM rejects a second add with
            # LimitExceeded (an instance profile holds at most one role).
            if e.response.get("Error", {}).get("Code", "") not in ("LimitExceeded", "EntityAlreadyExists"):
                raise
        self._wait_for_instance_profile_visible()

    def _wait_for_instance_profile_visible(self) -> None:
        """Poll until ``GetInstanceProfile`` reflects the attached role.

        IAM is eventually consistent: a freshly-created instance profile may not
        yet report its role to a subsequent describe (or to RunInstances). Poll a
        few times so the create path can attach the profile right after prepare.
        """
        if not poll_until(
            self._instance_profile_has_role,
            timeout=_IAM_CONSISTENCY_TIMEOUT_SECONDS,
            poll_interval=_IAM_CONSISTENCY_POLL_SECONDS,
        ):
            logger.warning(
                "IAM instance profile {} did not report its role within {}s; proceeding (it may attach shortly)",
                self.identity_name,
                _IAM_CONSISTENCY_TIMEOUT_SECONDS,
            )

    def _instance_profile_has_role(self) -> bool:
        response = self._iam().get_instance_profile(InstanceProfileName=self.identity_name)
        return bool(response.get("InstanceProfile", {}).get("Roles"))

    def _delete_instance_profile_if_present(self) -> None:
        try:
            response = self._iam().get_instance_profile(InstanceProfileName=self.identity_name)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code", "") in ("NoSuchEntity", "404"):
                return
            raise
        for role in response.get("InstanceProfile", {}).get("Roles", []):
            self._iam().remove_role_from_instance_profile(
                InstanceProfileName=self.identity_name, RoleName=role["RoleName"]
            )
        self._iam().delete_instance_profile(InstanceProfileName=self.identity_name)

    def _delete_role_if_present(self) -> None:
        try:
            self._iam().delete_role_policy(RoleName=self.identity_name, PolicyName=_HOST_IDENTITY_INLINE_POLICY_NAME)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code", "") not in ("NoSuchEntity", "404"):
                raise
        try:
            self._iam().delete_role(RoleName=self.identity_name)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code", "") not in ("NoSuchEntity", "404"):
                raise


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


# IAM is eventually consistent, so a just-created instance profile may not yet
# report its attached role. Bound how long ``ensure_host_identity`` waits for it.
_IAM_CONSISTENCY_TIMEOUT_SECONDS: Final[float] = 15.0
_IAM_CONSISTENCY_POLL_SECONDS: Final[float] = 1.0


@contextmanager
def _translate_s3_errors(bucket_name: str) -> Iterator[None]:
    """Translate ``botocore.ClientError`` into ``S3StateBucketError`` within the block."""
    try:
        yield
    except ClientError as e:
        raise S3StateBucketError(f"S3 operation on bucket {bucket_name!r} failed: {e}") from e


@contextmanager
def _translate_iam_errors(identity_name: str) -> Iterator[None]:
    """Translate ``botocore.ClientError`` into ``S3StateHostIdentityError`` within the block."""
    try:
        yield
    except ClientError as e:
        raise S3StateHostIdentityError(f"IAM operation on identity {identity_name!r} failed: {e}") from e
