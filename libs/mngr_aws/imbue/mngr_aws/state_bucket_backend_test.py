"""Tests for the AwsProvider's S3-state-bucket vs tag agent-data behavior."""

from datetime import datetime
from datetime import timezone

import boto3
from botocore.stub import Stubber

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.testing import capture_log_warnings
from imbue.mngr_aws.backend import AWS_BACKEND_NAME
from imbue.mngr_aws.backend import AwsProvider
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_aws.config import ExistingSecurityGroup
from imbue.mngr_aws.state_bucket import S3StateBucket
from imbue.mngr_aws.testing import _StubbedAwsVpsClient
from imbue.mngr_vps_docker.host_state_store import BucketHostStateStore
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord
from imbue.mngr_vps_docker.testing import seed_stopped_host_record

_BUCKET_NAME = "mngr-state-test-bucket"


def _build_bucket_provider(
    mngr_ctx: MngrContext, is_offline_host_dir_enabled: bool = True
) -> tuple[AwsProvider, Stubber]:
    """Build an AwsProvider configured with an S3 state bucket (moto-backed) + a stubbed EC2.

    The EC2 stubber is queued with no responses: a bucket-mode test must make
    NO EC2 tag calls, so any stray call surfaces as a stubber error.
    """
    config = AwsProviderConfig(
        backend=AWS_BACKEND_NAME,
        default_ami_id="ami-x",
        auto_shutdown_seconds=3600,
        state_bucket_name=_BUCKET_NAME,
        is_offline_host_dir_enabled=is_offline_host_dir_enabled,
    )
    session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1")
    ec2 = session.client("ec2", region_name="us-east-1")
    stubber = Stubber(ec2)
    client = _StubbedAwsVpsClient(
        session=session,
        region="us-east-1",
        ami_id="ami-x",
        security_group=ExistingSecurityGroup(id="sg-x"),
        stubbed_ec2_client=ec2,
    )
    provider = AwsProvider(
        name=ProviderInstanceName("aws-test"),
        host_dir=config.host_dir,
        mngr_ctx=mngr_ctx,
        config=config,
        vps_client=client,
        aws_client=client,
        aws_config=config,
    )
    # Pre-create the bucket directly (before resolving _state_bucket, which
    # caches None when the bucket does not yet exist) so writes land and the
    # provider's existence probe sees it.
    S3StateBucket(session=session, region="us-east-1", bucket_name=_BUCKET_NAME).ensure_bucket()
    bucket = provider._state_bucket
    assert bucket is not None
    return provider, stubber


def test_bucket_mode_persists_agent_to_bucket_and_writes_no_ec2_tags(
    aws_mock: None, temp_mngr_ctx: MngrContext
) -> None:
    """With a state bucket configured, agent data goes to the bucket and NO EC2 tags are written."""
    provider, stubber = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    seed_stopped_host_record(provider, host_id)
    big_labels = {"k": "v" * 1000}
    agent_data = {"id": str(agent_id), "name": "alpha", "type": "claude", "labels": big_labels}

    # The stubber has NO queued describe_instances/create_tags responses: if the
    # bucket path erroneously fell back to tags, the stubber would raise.
    stubber.activate()
    try:
        provider.persist_agent_data(host_id, agent_data)
        records = provider.list_persisted_agent_data_for_host(host_id)
    finally:
        stubber.deactivate()

    by_id = {r["id"]: r for r in records}
    assert str(agent_id) in by_id
    # The >256-char labels blob (which the tag mirror would drop) survives in the bucket.
    assert by_id[str(agent_id)]["labels"] == big_labels


def test_bucket_mode_mirrors_host_record_and_reconstructs_offline_host(
    aws_mock: None, temp_mngr_ctx: MngrContext
) -> None:
    """``_persist_host_record_externally`` writes the full record; ``to_offline_host`` reads it back."""
    provider, stubber = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    certified = CertifiedHostData(
        host_id=str(host_id),
        host_name="recovered-host",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        stop_reason=HostState.STOPPED.value,
    )
    record = VpsDockerHostRecord(certified_host_data=certified)

    provider._persist_host_record_externally(record)

    bucket = provider._state_bucket
    assert bucket is not None
    assert bucket.read_host_record_json(host_id) is not None

    # to_offline_host first tries the base SSH/volume path: its discovery sweep
    # lists instances (returns none here => HostNotFoundError), then the override
    # reconstructs the full record from the bucket.
    stubber.add_response("describe_instances", {"Reservations": []})
    stubber.activate()
    try:
        offline = provider.to_offline_host(host_id)
    finally:
        stubber.deactivate()
    assert str(offline.id) == str(host_id)
    assert offline.certified_host_data.host_name == "recovered-host"


def test_bucket_mode_remove_agent_clears_bucket_record(aws_mock: None, temp_mngr_ctx: MngrContext) -> None:
    provider, stubber = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    seed_stopped_host_record(provider, host_id)

    stubber.activate()
    try:
        provider.persist_agent_data(host_id, {"id": str(agent_id), "name": "alpha"})
        assert len(provider.list_persisted_agent_data_for_host(host_id)) == 1
        provider.remove_persisted_agent_data(host_id, agent_id)
        assert provider.list_persisted_agent_data_for_host(host_id) == []
    finally:
        stubber.deactivate()


def test_delete_host_externally_removes_bucket_state(aws_mock: None, temp_mngr_ctx: MngrContext) -> None:
    provider, _stubber = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    bucket = provider._state_bucket
    assert bucket is not None
    bucket.write_host_record_json(host_id, "{}")
    bucket.write_agent_record(host_id, "agent-1", {"id": "agent-1"})
    assert bucket.has_any_host_state() is True

    provider._delete_host_record_externally(host_id)
    assert bucket.has_any_host_state() is False


def test_state_store_is_bucket_store_when_bucket_present(aws_mock: None, temp_mngr_ctx: MngrContext) -> None:
    """With a state bucket configured, ``_state_store`` is the bucket-backed store (the sole offline store)."""
    provider, _stubber = _build_bucket_provider(temp_mngr_ctx)
    assert isinstance(provider._state_store, BucketHostStateStore)


# =============================================================================
# Offline host_dir volume (get_volume_for_host / get_volume_reference_for_host)
# =============================================================================


def test_get_volume_reference_is_cheap_and_scoped_to_host_dir(aws_mock: None, temp_mngr_ctx: MngrContext) -> None:
    """The reference getter returns a host_dir-scoped volume with no S3 probe."""
    provider, _stubber = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    bucket = provider._state_bucket
    assert bucket is not None
    # Seed a file under the host's host_dir prefix and confirm the reference reads it.
    hex_id = host_id.get_uuid().hex
    boto3_session = bucket.session
    boto3_session.client("s3", region_name="us-east-1").put_object(
        Bucket=_BUCKET_NAME, Key=f"hosts/{hex_id}/host_dir/events/e.jsonl", Body=b"evt"
    )
    reference = provider.get_volume_reference_for_host(host_id)
    assert reference is not None
    assert reference.volume.read_file("events/e.jsonl") == b"evt"


def test_get_volume_for_host_returns_none_when_prefix_empty(aws_mock: None, temp_mngr_ctx: MngrContext) -> None:
    """An empty host_dir prefix with no resolvable instance yields None and emits no diagnostic warning.

    Complement of ``..._warns_when_instance_has_no_iam_profile``: when the
    diagnostic can't even find the instance it returns early, so the user must not
    see the (misleading) 'no IAM profile' warning. Together the two pin the
    empty-prefix matrix (instance-without-profile -> warn; no-instance -> silent).
    """
    provider, stubber = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    # The diagnostic calls _find_instance_for_host, which lists instances, finds
    # no match, refreshes the cache once, and lists again -> two describe calls,
    # then returns early (no instance, so no IAM-profile probe).
    stubber.add_response("describe_instances", {"Reservations": []})
    stubber.add_response("describe_instances", {"Reservations": []})
    stubber.activate()
    try:
        with capture_log_warnings() as warnings:
            assert provider.get_volume_for_host(host_id) is None
    finally:
        stubber.deactivate()
    assert not any("no attached IAM instance profile" in message for message in warnings)


def test_get_volume_for_host_warns_when_instance_has_no_iam_profile(
    aws_mock: None, temp_mngr_ctx: MngrContext
) -> None:
    """Empty host_dir + an instance with no IAM profile -> a 're-run prepare' WARNING (non-fatal)."""
    provider, stubber = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    matching = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": "i-noprofile",
                        "State": {"Name": "stopped"},
                        "Tags": [{"Key": "mngr-host-id", "Value": str(host_id)}],
                    }
                ]
            }
        ]
    }
    # _find_instance_for_host: one list (cache populated this run on first call).
    stubber.add_response("describe_instances", matching)
    # get_instance_iam_profile_arn: a direct describe of the instance (no profile).
    stubber.add_response("describe_instances", matching)
    stubber.activate()
    try:
        with capture_log_warnings() as warnings:
            assert provider.get_volume_for_host(host_id) is None
    finally:
        stubber.deactivate()
    assert any("no attached IAM instance profile" in message for message in warnings)


def test_get_volume_for_host_returns_volume_when_objects_present(aws_mock: None, temp_mngr_ctx: MngrContext) -> None:
    """A non-empty host_dir prefix yields a readable volume."""
    provider, _stubber = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    bucket = provider._state_bucket
    assert bucket is not None
    hex_id = host_id.get_uuid().hex
    bucket.session.client("s3", region_name="us-east-1").put_object(
        Bucket=_BUCKET_NAME, Key=f"hosts/{hex_id}/host_dir/logs/a.log", Body=b"a"
    )
    volume = provider.get_volume_for_host(host_id)
    assert volume is not None
    assert volume.volume.read_file("logs/a.log") == b"a"


def test_host_dir_sync_instance_profile_returned_when_identity_exists(
    aws_mock: None, temp_mngr_ctx: MngrContext
) -> None:
    """The create path attaches the provisioned identity's profile when it exists."""
    provider, _stubber = _build_bucket_provider(temp_mngr_ctx)
    identity = provider._host_identity()
    assert identity is not None
    identity.ensure_host_identity()
    assert provider._host_dir_sync_instance_profile() == identity.identity_name


def test_host_dir_sync_instance_profile_none_when_identity_absent(aws_mock: None, temp_mngr_ctx: MngrContext) -> None:
    """No attachment (None) when the identity was never provisioned by prepare."""
    provider, _stubber = _build_bucket_provider(temp_mngr_ctx)
    assert provider._host_dir_sync_instance_profile() is None


def test_host_dir_sync_instance_profile_none_when_feature_disabled(aws_mock: None, temp_mngr_ctx: MngrContext) -> None:
    """No attachment when host_dir sync is disabled, even if the identity exists."""
    provider, _stubber = _build_bucket_provider(temp_mngr_ctx, is_offline_host_dir_enabled=False)
    identity = provider._host_identity()
    assert identity is not None
    identity.ensure_host_identity()
    assert provider._host_dir_sync_instance_profile() is None


def test_get_volume_reference_is_none_when_feature_disabled(aws_mock: None, temp_mngr_ctx: MngrContext) -> None:
    """With is_offline_host_dir_enabled=False, no offline host_dir volume is returned."""
    provider, _stubber = _build_bucket_provider(temp_mngr_ctx, is_offline_host_dir_enabled=False)
    assert provider.get_volume_reference_for_host(HostId.generate()) is None
    assert provider.get_volume_for_host(HostId.generate()) is None
