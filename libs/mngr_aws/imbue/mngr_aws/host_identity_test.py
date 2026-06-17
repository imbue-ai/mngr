"""Unit tests for ``S3StateHostIdentity`` (IAM role + instance profile) using moto."""

import json

import boto3

from imbue.mngr_aws.state_bucket import S3StateHostIdentity
from imbue.mngr_aws.state_bucket import build_host_identity_inline_policy
from imbue.mngr_aws.state_bucket import host_identity_name_for_bucket

_US_EAST_1 = "us-east-1"
_BUCKET = "mngr-state-123456789012-us-east-1"


def _identity(session: boto3.Session) -> S3StateHostIdentity:
    return S3StateHostIdentity(session=session, region=_US_EAST_1, bucket_name=_BUCKET)


def test_identity_name_is_deterministic_from_bucket() -> None:
    assert host_identity_name_for_bucket(_BUCKET) == f"mngr-aws-host-{_BUCKET}"


def test_inline_policy_is_least_privilege_scoped_to_hosts_prefix() -> None:
    policy = json.loads(build_host_identity_inline_policy(_BUCKET))
    statements = {s["Sid"]: s for s in policy["Statement"]}
    # ListBucket only on the bucket ARN itself.
    assert statements["ListStateBucket"]["Action"] == ["s3:ListBucket"]
    assert statements["ListStateBucket"]["Resource"] == [f"arn:aws:s3:::{_BUCKET}"]
    # Object actions only under the hosts/* prefix -- never the whole bucket.
    rw = statements["ReadWriteHostObjects"]
    assert set(rw["Action"]) == {"s3:PutObject", "s3:GetObject", "s3:DeleteObject"}
    assert rw["Resource"] == [f"arn:aws:s3:::{_BUCKET}/hosts/*"]


def test_ensure_host_identity_creates_role_profile_and_policy(aws_session: boto3.Session) -> None:
    identity = _identity(aws_session)
    assert identity.host_identity_exists() is False
    profile_name = identity.ensure_host_identity()
    assert profile_name == host_identity_name_for_bucket(_BUCKET)
    assert identity.host_identity_exists() is True

    iam = aws_session.client("iam", region_name=_US_EAST_1)
    # The instance profile holds the role.
    profile = iam.get_instance_profile(InstanceProfileName=profile_name)["InstanceProfile"]
    assert [r["RoleName"] for r in profile["Roles"]] == [profile_name]
    # The role trusts ec2.amazonaws.com.
    role = iam.get_role(RoleName=profile_name)["Role"]
    trust = role["AssumeRolePolicyDocument"]
    assert trust["Statement"][0]["Principal"]["Service"] == "ec2.amazonaws.com"
    # The least-privilege inline policy is attached.
    policy = iam.get_role_policy(RoleName=profile_name, PolicyName="mngr-host-dir-sync")
    assert policy["PolicyDocument"]["Statement"][1]["Resource"] == [f"arn:aws:s3:::{_BUCKET}/hosts/*"]


def test_ensure_host_identity_is_idempotent(aws_session: boto3.Session) -> None:
    identity = _identity(aws_session)
    first = identity.ensure_host_identity()
    # A second call must not error and must return the same name.
    assert identity.ensure_host_identity() == first


def test_delete_host_identity_removes_role_and_profile(aws_session: boto3.Session) -> None:
    identity = _identity(aws_session)
    identity.ensure_host_identity()
    identity.delete_host_identity()
    assert identity.host_identity_exists() is False
    # Deleting an already-absent identity is idempotent.
    identity.delete_host_identity()


def test_host_identity_exists_false_before_create(aws_session: boto3.Session) -> None:
    assert _identity(aws_session).host_identity_exists() is False
