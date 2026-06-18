"""Unit tests for ``S3StateBucket`` using moto's in-memory S3."""

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from imbue.mngr.primitives import HostId
from imbue.mngr_aws.state_bucket import S3StateBucket
from imbue.mngr_aws.state_bucket import S3StateBucketError
from imbue.mngr_aws.state_bucket import _raise_on_delete_errors

_US_EAST_1 = "us-east-1"
_OTHER_REGION = "us-west-2"


def _make_bucket(session: boto3.Session, region: str, bucket_name: str) -> S3StateBucket:
    bucket = S3StateBucket(session=session, region=region, bucket_name=bucket_name)
    bucket.ensure_bucket()
    return bucket


def test_ensure_bucket_creates_in_us_east_1(aws_session: boto3.Session) -> None:
    bucket = S3StateBucket(session=aws_session, region=_US_EAST_1, bucket_name="mngr-state-bucket-east")
    assert bucket.bucket_exists() is False
    assert bucket.ensure_bucket() is True
    assert bucket.bucket_exists() is True


def test_ensure_bucket_is_idempotent(aws_session: boto3.Session) -> None:
    bucket = S3StateBucket(session=aws_session, region=_US_EAST_1, bucket_name="mngr-state-idempotent")
    assert bucket.ensure_bucket() is True
    # Second call must not re-create: returns False (already existed).
    assert bucket.ensure_bucket() is False


def test_ensure_bucket_treats_concurrent_create_as_idempotent(aws_session: boto3.Session) -> None:
    # A racing concurrent `prepare` (the HeadBucket saw 404, then someone else
    # created it first) makes create_bucket raise BucketAlreadyOwnedByYou. The
    # bucket is ours, so ensure_bucket must apply its hardening config and report
    # not-created rather than raising.
    bucket = S3StateBucket(session=aws_session, region=_US_EAST_1, bucket_name="mngr-state-raced")
    with Stubber(bucket._s3()) as stubber:
        stubber.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
        stubber.add_client_error("create_bucket", service_error_code="BucketAlreadyOwnedByYou", http_status_code=409)
        stubber.add_response("put_public_access_block", {})
        stubber.add_response("put_bucket_encryption", {})
        stubber.add_response("put_bucket_tagging", {})
        assert bucket.ensure_bucket() is False
        stubber.assert_no_pending_responses()


def test_ensure_bucket_raises_on_other_create_error(aws_session: boto3.Session) -> None:
    # A create error that is not BucketAlreadyOwnedByYou (e.g. a permission
    # denial) must surface as S3StateBucketError, not be swallowed.
    bucket = S3StateBucket(session=aws_session, region=_US_EAST_1, bucket_name="mngr-state-denied")
    with Stubber(bucket._s3()) as stubber:
        stubber.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
        stubber.add_client_error("create_bucket", service_error_code="AccessDenied", http_status_code=403)
        with pytest.raises(S3StateBucketError, match="mngr-state-denied"):
            bucket.ensure_bucket()


def test_ensure_bucket_creates_in_other_region_with_location_constraint() -> None:
    with mock_aws():
        session = boto3.Session(
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
            region_name=_OTHER_REGION,
        )
        bucket = S3StateBucket(session=session, region=_OTHER_REGION, bucket_name="mngr-state-west")
        assert bucket.ensure_bucket() is True
        # moto records the LocationConstraint; verify the bucket landed in the region.
        location = session.client("s3", region_name=_OTHER_REGION).get_bucket_location(Bucket="mngr-state-west")
        assert location["LocationConstraint"] == _OTHER_REGION


def test_host_record_round_trip(aws_session: boto3.Session) -> None:
    bucket = _make_bucket(aws_session, _US_EAST_1, "mngr-state-record")
    host_id = HostId.generate()
    assert bucket.read_host_record_json(host_id) is None
    record_json = '{"certified_host_data": {"host_id": "x"}}'
    bucket.write_host_record_json(host_id, record_json)
    assert bucket.read_host_record_json(host_id) == record_json


def test_agent_records_round_trip_and_remove(aws_session: boto3.Session) -> None:
    bucket = _make_bucket(aws_session, _US_EAST_1, "mngr-state-agents")
    host_id = HostId.generate()
    assert bucket.list_agent_records(host_id) == []
    # A labels blob far larger than the 256-char EC2 tag limit must survive.
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


def test_delete_host_state_removes_record_and_agents(aws_session: boto3.Session) -> None:
    bucket = _make_bucket(aws_session, _US_EAST_1, "mngr-state-delete")
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


def test_has_any_host_state_isolated_per_host(aws_session: boto3.Session) -> None:
    bucket = _make_bucket(aws_session, _US_EAST_1, "mngr-state-multi")
    assert bucket.has_any_host_state() is False
    host_a = HostId.generate()
    host_b = HostId.generate()
    bucket.write_host_record_json(host_a, "{}")
    assert bucket.has_any_host_state() is True
    bucket.delete_host_state(host_b)
    # Deleting an unrelated empty host leaves host_a's state intact.
    assert bucket.has_any_host_state() is True


def test_delete_bucket_empties_then_deletes(aws_session: boto3.Session) -> None:
    bucket = _make_bucket(aws_session, _US_EAST_1, "mngr-state-teardown")
    host_id = HostId.generate()
    bucket.write_host_record_json(host_id, "{}")
    bucket.delete_bucket()
    assert bucket.bucket_exists() is False
    # Deleting an already-absent bucket is idempotent.
    bucket.delete_bucket()


def test_raise_on_delete_errors_surfaces_per_key_failures() -> None:
    # DeleteObjects returns HTTP 200 with per-key failures in the Errors array
    # (no ClientError), so a partial delete must be surfaced rather than swallowed.
    response = {
        "Deleted": [{"Key": "hosts/abc/host_state.json"}],
        "Errors": [{"Key": "hosts/abc/agents/x.json", "Code": "AccessDenied", "Message": "denied"}],
    }
    with pytest.raises(S3StateBucketError) as exc_info:
        _raise_on_delete_errors(response, "mngr-state-bucket")
    assert "agents/x.json" in str(exc_info.value)
    assert "AccessDenied" in str(exc_info.value)


def test_raise_on_delete_errors_noop_on_clean_response() -> None:
    _raise_on_delete_errors({"Deleted": [{"Key": "hosts/abc/host_state.json"}]}, "mngr-state-bucket")
