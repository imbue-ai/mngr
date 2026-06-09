"""Tests for the AWS EC2 client.

We use ``botocore.stub.Stubber`` to script EC2 API responses without making
real API calls. The fixture below creates a real boto3 session and EC2 client
and wraps it in a stubber so each test can declaratively queue expected
requests and canned responses.
"""

from collections.abc import Iterator
from datetime import datetime
from datetime import timezone

import boto3
import pytest
from botocore.stub import ANY
from botocore.stub import Stubber

from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import AutoCreateSecurityGroup
from imbue.mngr_aws.config import ExistingSecurityGroup
from imbue.mngr_aws.testing import _StubbedAwsVpsClient
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.errors import VpsProvisioningError
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus
from imbue.mngr_vps_docker.primitives import VpsSnapshotId


@pytest.fixture()
def stubbed_client() -> Iterator[tuple[AwsVpsClient, Stubber]]:
    """Yield a _StubbedAwsVpsClient whose underlying EC2 client is wrapped in a Stubber.

    Uses the test-only ``_StubbedAwsVpsClient`` subclass (defined in
    ``mngr_aws.testing``) so the production ``AwsVpsClient`` does not carry
    a field whose sole purpose is test orchestration.
    """
    session = boto3.Session(
        aws_access_key_id="AKIATEST",
        aws_secret_access_key="secret",
        region_name="us-east-1",
    )
    ec2 = session.client("ec2", region_name="us-east-1")
    stubber = Stubber(ec2)

    client = _StubbedAwsVpsClient(
        session=session,
        region="us-east-1",
        ami_id="ami-test12345",
        security_group=ExistingSecurityGroup(id="sg-test"),
        stubbed_ec2_client=ec2,
    )

    stubber.activate()
    try:
        yield client, stubber
    finally:
        stubber.deactivate()


@pytest.fixture()
def auto_sg_client() -> Iterator[tuple[AwsVpsClient, Stubber]]:
    """Like ``stubbed_client`` but with ``AutoCreateSecurityGroup`` and a tight CIDR.

    Used by the ensure_security_group tests below, which need the
    auto-create code path. The CIDR is the RFC 5737 documentation-only
    range so the value can never resemble a real IP.
    """
    session = boto3.Session(
        aws_access_key_id="AKIATEST",
        aws_secret_access_key="secret",
        region_name="us-east-1",
    )
    ec2 = session.client("ec2", region_name="us-east-1")
    stubber = Stubber(ec2)
    client = _StubbedAwsVpsClient(
        session=session,
        region="us-east-1",
        ami_id="ami-test",
        security_group=AutoCreateSecurityGroup(name="mngr-aws-test"),
        allowed_ssh_cidrs=("203.0.113.4/32",),
        stubbed_ec2_client=ec2,
    )
    stubber.activate()
    try:
        yield client, stubber
    finally:
        stubber.deactivate()


# =============================================================================
# create_instance
# =============================================================================


def test_create_instance(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    client, stubber = stubbed_client
    stubber.add_response(
        "run_instances",
        {"Instances": [{"InstanceId": "i-0abc123def456"}]},
        expected_params={
            "ImageId": "ami-test12345",
            "InstanceType": "t3.small",
            "MinCount": 1,
            "MaxCount": 1,
            "UserData": "test-user-data",
            "BlockDeviceMappings": ANY,
            "NetworkInterfaces": ANY,
            "InstanceInitiatedShutdownBehavior": "terminate",
            "MetadataOptions": ANY,
            "TagSpecifications": ANY,
            "KeyName": "key-1",
        },
    )
    instance_id = client.create_instance(
        label="mngr-test-aws-host",
        region="us-east-1",
        plan="t3.small",
        user_data="test-user-data",
        ssh_key_ids=["key-1"],
        tags={"mngr-provider": "test", "mngr-host-id": "h1"},
    )
    assert instance_id == VpsInstanceId("i-0abc123def456")


def test_create_instance_no_instances_raises(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    client, stubber = stubbed_client
    stubber.add_response("run_instances", {"Instances": []})
    with pytest.raises(VpsProvisioningError):
        client.create_instance(
            label="mngr-test-aws-host",
            region="us-east-1",
            plan="t3.small",
            user_data="test",
            ssh_key_ids=[],
            tags={},
        )


def test_create_instance_cross_region_raises(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    client, _stubber = stubbed_client
    with pytest.raises(VpsApiError, match="Cross-region create not supported"):
        client.create_instance(
            label="mngr-test-aws-host",
            region="eu-west-1",
            plan="t3.small",
            user_data="test",
            ssh_key_ids=[],
            tags={},
        )


# =============================================================================
# destroy_instance / get_instance_status / get_instance_ip / list_instances
# =============================================================================


def test_destroy_instance(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    client, stubber = stubbed_client
    stubber.add_response(
        "terminate_instances",
        {"TerminatingInstances": [{"InstanceId": "i-abc"}]},
        expected_params={"InstanceIds": ["i-abc"]},
    )
    client.destroy_instance(VpsInstanceId("i-abc"))


def test_get_instance_status_running(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    client, stubber = stubbed_client
    stubber.add_response(
        "describe_instances",
        {"Reservations": [{"Instances": [{"InstanceId": "i-1", "State": {"Name": "running"}}]}]},
        expected_params={"InstanceIds": ["i-1"]},
    )
    assert client.get_instance_status(VpsInstanceId("i-1")) == VpsInstanceStatus.ACTIVE


def test_get_instance_status_stopped(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    client, stubber = stubbed_client
    stubber.add_response(
        "describe_instances",
        {"Reservations": [{"Instances": [{"InstanceId": "i-1", "State": {"Name": "stopped"}}]}]},
        expected_params={"InstanceIds": ["i-1"]},
    )
    assert client.get_instance_status(VpsInstanceId("i-1")) == VpsInstanceStatus.HALTED


def test_get_instance_status_pending(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    client, stubber = stubbed_client
    stubber.add_response(
        "describe_instances",
        {"Reservations": [{"Instances": [{"InstanceId": "i-1", "State": {"Name": "pending"}}]}]},
        expected_params={"InstanceIds": ["i-1"]},
    )
    assert client.get_instance_status(VpsInstanceId("i-1")) == VpsInstanceStatus.PENDING


def test_get_instance_status_no_reservations(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    client, stubber = stubbed_client
    stubber.add_response(
        "describe_instances",
        {"Reservations": []},
        expected_params={"InstanceIds": ["i-1"]},
    )
    assert client.get_instance_status(VpsInstanceId("i-1")) == VpsInstanceStatus.UNKNOWN


def test_get_instance_status_instance_not_found_returns_unknown(
    stubbed_client: tuple[AwsVpsClient, Stubber],
) -> None:
    """The specific InvalidInstanceID.NotFound code maps to UNKNOWN (instance deleted upstream)."""
    client, stubber = stubbed_client
    stubber.add_client_error(
        "describe_instances",
        service_error_code="InvalidInstanceID.NotFound",
        service_message="The instance ID 'i-missing' does not exist",
        http_status_code=400,
        expected_params={"InstanceIds": ["i-missing"]},
    )
    assert client.get_instance_status(VpsInstanceId("i-missing")) == VpsInstanceStatus.UNKNOWN


def test_get_instance_status_unrelated_not_found_surfaces(
    stubbed_client: tuple[AwsVpsClient, Stubber],
) -> None:
    """Regression: other ``*.NotFound`` codes must NOT be silently swallowed as UNKNOWN."""
    client, stubber = stubbed_client
    stubber.add_client_error(
        "describe_instances",
        service_error_code="InvalidSubnetID.NotFound",
        service_message="The subnet ID 'subnet-x' does not exist",
        http_status_code=400,
        expected_params={"InstanceIds": ["i-1"]},
    )
    with pytest.raises(VpsApiError, match="InvalidSubnetID.NotFound"):
        client.get_instance_status(VpsInstanceId("i-1"))


def test_get_instance_ip(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    client, stubber = stubbed_client
    stubber.add_response(
        "describe_instances",
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-1",
                            "State": {"Name": "running"},
                            "PublicIpAddress": "1.2.3.4",
                        }
                    ]
                }
            ]
        },
        expected_params={"InstanceIds": ["i-1"]},
    )
    assert client.get_instance_ip(VpsInstanceId("i-1")) == "1.2.3.4"


def test_get_instance_ip_not_ready(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    client, stubber = stubbed_client
    stubber.add_response(
        "describe_instances",
        {"Reservations": [{"Instances": [{"InstanceId": "i-1", "State": {"Name": "pending"}}]}]},
        expected_params={"InstanceIds": ["i-1"]},
    )
    with pytest.raises(VpsProvisioningError):
        client.get_instance_ip(VpsInstanceId("i-1"))


def test_list_instances_filters_by_provider_tag(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    client, stubber = stubbed_client
    stubber.add_response(
        "describe_instances",
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-1",
                            "State": {"Name": "running"},
                            "PublicIpAddress": "10.0.0.1",
                            "Tags": [
                                {"Key": "mngr-provider", "Value": "test"},
                                {"Key": "mngr-host-id", "Value": "h1"},
                            ],
                        }
                    ]
                }
            ]
        },
        expected_params={
            "Filters": [
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
                {"Name": "tag:mngr-provider", "Values": ["test"]},
            ]
        },
    )
    instances = client.list_instances(provider_tag="test")
    assert len(instances) == 1
    assert instances[0]["id"] == "i-1"
    assert instances[0]["main_ip"] == "10.0.0.1"
    assert "mngr-provider=test" in instances[0]["tags"]


def test_list_instances_translates_client_errors(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    """Regression: ClientError raised during pagination must surface as VpsApiError.

    Without translation, a raw botocore.ClientError would bypass the
    (HostConnectionError, MngrError) handler in the shared discovery
    flow and crash listing instead of being surfaced as a warning.
    """
    client, stubber = stubbed_client
    stubber.add_client_error(
        "describe_instances",
        service_error_code="UnauthorizedOperation",
        service_message="not authorized",
        http_status_code=403,
    )
    with pytest.raises(VpsApiError, match="UnauthorizedOperation"):
        client.list_instances(provider_tag="test")


# =============================================================================
# Key pairs
# =============================================================================


def test_upload_ssh_key(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    client, stubber = stubbed_client
    stubber.add_response(
        "import_key_pair",
        {"KeyName": "mngr-test-h1", "KeyFingerprint": "ab:cd"},
        expected_params={"KeyName": "mngr-test-h1", "PublicKeyMaterial": b"ssh-ed25519 AAAA test"},
    )
    key_id = client.upload_ssh_key("mngr-test-h1", "ssh-ed25519 AAAA test")
    assert key_id == "mngr-test-h1"


def test_delete_ssh_key(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    client, stubber = stubbed_client
    stubber.add_response(
        "delete_key_pair",
        {},
        expected_params={"KeyName": "mngr-test-h1"},
    )
    client.delete_ssh_key("mngr-test-h1")


def test_list_ssh_keys(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    client, stubber = stubbed_client
    stubber.add_response(
        "describe_key_pairs",
        {"KeyPairs": [{"KeyName": "k1", "KeyFingerprint": "aa"}, {"KeyName": "k2", "KeyFingerprint": "bb"}]},
    )
    keys = client.list_ssh_keys()
    assert len(keys) == 2
    assert keys[0].id == "k1"
    assert keys[0].name == "k1"


# =============================================================================
# Snapshots
# =============================================================================


def test_list_snapshots_empty(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    client, stubber = stubbed_client
    stubber.add_response(
        "describe_snapshots",
        {"Snapshots": []},
        expected_params={"OwnerIds": ["self"]},
    )
    assert client.list_snapshots() == []


def test_list_snapshots(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    client, stubber = stubbed_client
    stubber.add_response(
        "describe_snapshots",
        {
            "Snapshots": [
                {
                    "SnapshotId": "snap-1",
                    "Description": "test snapshot",
                    "StartTime": datetime(2026, 1, 1, tzinfo=timezone.utc),
                }
            ]
        },
        expected_params={"OwnerIds": ["self"]},
    )
    snapshots = client.list_snapshots()
    assert len(snapshots) == 1
    assert snapshots[0].id == VpsSnapshotId("snap-1")
    assert snapshots[0].description == "test snapshot"


def test_delete_snapshot(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    client, stubber = stubbed_client
    stubber.add_response(
        "delete_snapshot",
        {},
        expected_params={"SnapshotId": "snap-1"},
    )
    client.delete_snapshot(VpsSnapshotId("snap-1"))


# =============================================================================
# ensure_security_group
# =============================================================================


def test_ensure_security_group_returns_preset_id_when_provided(
    stubbed_client: tuple[AwsVpsClient, Stubber],
) -> None:
    client, _stubber = stubbed_client
    assert client.ensure_security_group() == "sg-test"


def test_ensure_security_group_auto_create_fails_closed_when_no_cidrs() -> None:
    """AutoCreateSecurityGroup + empty allowed_ssh_cidrs must raise, not create an unreachable SG."""
    session = boto3.Session(
        aws_access_key_id="AKIATEST",
        aws_secret_access_key="secret",
        region_name="us-east-1",
    )
    ec2 = session.client("ec2", region_name="us-east-1")
    client = _StubbedAwsVpsClient(
        session=session,
        provider_name=ProviderInstanceName("aws-west"),
        region="us-east-1",
        ami_id="ami-test",
        # Explicit default mirroring config.py
        security_group=AutoCreateSecurityGroup(),
        stubbed_ec2_client=ec2,
    )
    with pytest.raises(MngrError) as exc_info:
        client.ensure_security_group()
    message = str(exc_info.value)
    assert "allowed_ssh_cidrs is empty" in message
    # The error must be actionable: a runnable `mngr config set` command, keyed on
    # this provider's name, at user scope, that grabs the operator's public IP.
    assert "mngr config set providers.aws-west.allowed_ssh_cidrs" in message
    assert "--scope user" in message
    assert "curl -fsS https://checkip.amazonaws.com" in message


def test_ensure_security_group_reuses_existing_sg_when_found(
    auto_sg_client: tuple[AwsVpsClient, Stubber],
) -> None:
    client, stubber = auto_sg_client
    stubber.add_response(
        "describe_security_groups",
        {"SecurityGroups": [{"GroupId": "sg-existing", "GroupName": "mngr-aws-test"}]},
        expected_params={"Filters": [{"Name": "group-name", "Values": ["mngr-aws-test"]}]},
    )
    # One call per port: tcp/22 and tcp/container_ssh_port (default 2222).
    stubber.add_response(
        "authorize_security_group_ingress",
        {},
        expected_params={"GroupId": "sg-existing", "IpPermissions": ANY},
    )
    stubber.add_response(
        "authorize_security_group_ingress",
        {},
        expected_params={"GroupId": "sg-existing", "IpPermissions": ANY},
    )
    assert client.ensure_security_group() == "sg-existing"


def test_ensure_security_group_creates_sg_when_missing(
    auto_sg_client: tuple[AwsVpsClient, Stubber],
) -> None:
    client, stubber = auto_sg_client
    stubber.add_response(
        "describe_security_groups",
        {"SecurityGroups": []},
        expected_params={"Filters": [{"Name": "group-name", "Values": ["mngr-aws-test"]}]},
    )
    stubber.add_response(
        "create_security_group",
        {"GroupId": "sg-new"},
        expected_params={"GroupName": "mngr-aws-test", "Description": ANY},
    )
    # One call per port: tcp/22 and tcp/container_ssh_port (default 2222).
    stubber.add_response(
        "authorize_security_group_ingress",
        {},
        expected_params={"GroupId": "sg-new", "IpPermissions": ANY},
    )
    stubber.add_response(
        "authorize_security_group_ingress",
        {},
        expected_params={"GroupId": "sg-new", "IpPermissions": ANY},
    )
    assert client.ensure_security_group() == "sg-new"


def test_ensure_security_group_duplicate_on_one_port_does_not_drop_the_other(
    auto_sg_client: tuple[AwsVpsClient, Stubber],
) -> None:
    """Regression: prior implementation batched both ports and lost the second when one was a duplicate."""
    client, stubber = auto_sg_client
    stubber.add_response(
        "describe_security_groups",
        {"SecurityGroups": [{"GroupId": "sg-existing", "GroupName": "mngr-aws-test"}]},
        expected_params={"Filters": [{"Name": "group-name", "Values": ["mngr-aws-test"]}]},
    )
    # tcp/22 already exists -> duplicate (swallowed). tcp/container_ssh_port still gets added.
    stubber.add_client_error(
        "authorize_security_group_ingress",
        service_error_code="InvalidPermission.Duplicate",
        service_message="permission already exists",
        http_status_code=400,
        expected_params={"GroupId": "sg-existing", "IpPermissions": ANY},
    )
    stubber.add_response(
        "authorize_security_group_ingress",
        {},
        expected_params={"GroupId": "sg-existing", "IpPermissions": ANY},
    )
    assert client.ensure_security_group() == "sg-existing"
