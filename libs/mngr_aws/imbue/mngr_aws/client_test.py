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


@pytest.fixture()
def iam_stubbed_client() -> Iterator[tuple[AwsVpsClient, Stubber]]:
    """Yield a _StubbedAwsVpsClient whose underlying IAM client is wrapped in a Stubber.

    The self-stop instance-profile methods only call IAM, so this fixture wires
    a Stubber around a real ``iam`` client (the EC2 client is a bare stubbed
    client that no IAM test exercises). IAM is a global service, so the client
    is built without a region.
    """
    session = boto3.Session(
        aws_access_key_id="AKIATEST",
        aws_secret_access_key="secret",
        region_name="us-east-1",
    )
    ec2 = session.client("ec2", region_name="us-east-1")
    iam = session.client("iam")
    iam_stubber = Stubber(iam)
    client = _StubbedAwsVpsClient(
        session=session,
        region="us-east-1",
        ami_id="ami-test",
        security_group=AutoCreateSecurityGroup(name="mngr-aws-test"),
        stubbed_ec2_client=ec2,
        stubbed_iam_client=iam,
    )
    iam_stubber.activate()
    try:
        yield client, iam_stubber
    finally:
        iam_stubber.deactivate()


# =============================================================================
# ensure_self_stop_instance_profile / delete_self_stop_instance_profile
# =============================================================================


def _add_get_role_response(stubber: Stubber) -> None:
    """Queue a minimal create_role response (botocore requires the Role shape)."""
    stubber.add_response(
        "create_role",
        {
            "Role": {
                "Path": "/",
                "RoleName": "mngr-aws",
                "RoleId": "AROATESTAROATEST00",
                "Arn": "arn:aws:iam::123456789012:role/mngr-aws",
                "CreateDate": datetime(2026, 1, 1, tzinfo=timezone.utc),
            }
        },
        expected_params={"RoleName": "mngr-aws", "AssumeRolePolicyDocument": ANY},
    )


def test_ensure_self_stop_instance_profile_creates_all(
    iam_stubbed_client: tuple[AwsVpsClient, Stubber],
) -> None:
    """When nothing exists yet, all four IAM calls fire and the profile name is returned."""
    client, stubber = iam_stubbed_client
    _add_get_role_response(stubber)
    stubber.add_response(
        "put_role_policy",
        {},
        expected_params={"RoleName": "mngr-aws", "PolicyName": "mngr-aws", "PolicyDocument": ANY},
    )
    stubber.add_response(
        "create_instance_profile",
        {
            "InstanceProfile": {
                "Path": "/",
                "InstanceProfileName": "mngr-aws",
                "InstanceProfileId": "AIPATESTAIPATEST00",
                "Arn": "arn:aws:iam::123456789012:instance-profile/mngr-aws",
                "CreateDate": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "Roles": [],
            }
        },
        expected_params={"InstanceProfileName": "mngr-aws"},
    )
    stubber.add_response(
        "add_role_to_instance_profile",
        {},
        expected_params={"InstanceProfileName": "mngr-aws", "RoleName": "mngr-aws"},
    )
    assert client.ensure_self_stop_instance_profile() == "mngr-aws"
    stubber.assert_no_pending_responses()


def test_ensure_self_stop_instance_profile_idempotent_when_exists(
    iam_stubbed_client: tuple[AwsVpsClient, Stubber],
) -> None:
    """Already-existing role/profile (and already-attached role) is treated as success, no raise."""
    client, stubber = iam_stubbed_client
    stubber.add_client_error(
        "create_role",
        service_error_code="EntityAlreadyExists",
        service_message="Role with name mngr-aws already exists.",
        http_status_code=409,
        expected_params={"RoleName": "mngr-aws", "AssumeRolePolicyDocument": ANY},
    )
    stubber.add_response(
        "put_role_policy",
        {},
        expected_params={"RoleName": "mngr-aws", "PolicyName": "mngr-aws", "PolicyDocument": ANY},
    )
    stubber.add_client_error(
        "create_instance_profile",
        service_error_code="EntityAlreadyExists",
        service_message="Instance Profile mngr-aws already exists.",
        http_status_code=409,
        expected_params={"InstanceProfileName": "mngr-aws"},
    )
    stubber.add_client_error(
        "add_role_to_instance_profile",
        service_error_code="LimitExceeded",
        service_message="Cannot exceed quota for InstanceSessionsPerInstanceProfile.",
        http_status_code=409,
        expected_params={"InstanceProfileName": "mngr-aws", "RoleName": "mngr-aws"},
    )
    assert client.ensure_self_stop_instance_profile() == "mngr-aws"
    stubber.assert_no_pending_responses()


def test_ensure_self_stop_instance_profile_translates_other_errors(
    iam_stubbed_client: tuple[AwsVpsClient, Stubber],
) -> None:
    """A non-already-exists IAM error surfaces as VpsApiError (e.g. an access denial)."""
    client, stubber = iam_stubbed_client
    stubber.add_client_error(
        "create_role",
        service_error_code="AccessDenied",
        service_message="not authorized to perform iam:CreateRole",
        http_status_code=403,
        expected_params={"RoleName": "mngr-aws", "AssumeRolePolicyDocument": ANY},
    )
    with pytest.raises(VpsApiError, match="AccessDenied"):
        client.ensure_self_stop_instance_profile()


def test_delete_self_stop_instance_profile_deletes_all(
    iam_stubbed_client: tuple[AwsVpsClient, Stubber],
) -> None:
    """When all resources exist, the four deletes fire and the name is returned."""
    client, stubber = iam_stubbed_client
    stubber.add_response(
        "remove_role_from_instance_profile",
        {},
        expected_params={"InstanceProfileName": "mngr-aws", "RoleName": "mngr-aws"},
    )
    stubber.add_response(
        "delete_instance_profile",
        {},
        expected_params={"InstanceProfileName": "mngr-aws"},
    )
    stubber.add_response(
        "delete_role_policy",
        {},
        expected_params={"RoleName": "mngr-aws", "PolicyName": "mngr-aws"},
    )
    stubber.add_response("delete_role", {}, expected_params={"RoleName": "mngr-aws"})
    assert client.delete_self_stop_instance_profile() == "mngr-aws"
    stubber.assert_no_pending_responses()


def test_delete_self_stop_instance_profile_is_noop_when_absent(
    iam_stubbed_client: tuple[AwsVpsClient, Stubber],
) -> None:
    """Every step swallowing NoSuchEntity means a fully-clean account returns None, no raise."""
    client, stubber = iam_stubbed_client
    for operation, params in (
        ("remove_role_from_instance_profile", {"InstanceProfileName": "mngr-aws", "RoleName": "mngr-aws"}),
        ("delete_instance_profile", {"InstanceProfileName": "mngr-aws"}),
        ("delete_role_policy", {"RoleName": "mngr-aws", "PolicyName": "mngr-aws"}),
        ("delete_role", {"RoleName": "mngr-aws"}),
    ):
        stubber.add_client_error(
            operation,
            service_error_code="NoSuchEntity",
            service_message="The entity does not exist.",
            http_status_code=404,
            expected_params=params,
        )
    assert client.delete_self_stop_instance_profile() is None
    stubber.assert_no_pending_responses()


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


def test_create_instance_uses_ami_id_override(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    """``ami_id_override`` (from --aws-ami=) wins over the client's configured default AMI."""
    client, stubber = stubbed_client
    stubber.add_response(
        "run_instances",
        {"Instances": [{"InstanceId": "i-override"}]},
        # Critical assertion: the override (not the client's default ami-test12345) is sent.
        expected_params={
            "ImageId": "ami-override-xyz",
            "InstanceType": "t3.small",
            "MinCount": 1,
            "MaxCount": 1,
            "UserData": "test-user-data",
            "BlockDeviceMappings": ANY,
            "NetworkInterfaces": ANY,
            "InstanceInitiatedShutdownBehavior": "terminate",
            "MetadataOptions": ANY,
            "TagSpecifications": ANY,
        },
    )
    instance_id = client.create_instance(
        label="mngr-test-aws-host",
        region="us-east-1",
        plan="t3.small",
        user_data="test-user-data",
        ssh_key_ids=[],
        tags={},
        ami_id_override="ami-override-xyz",
    )
    assert instance_id == VpsInstanceId("i-override")


def test_create_instance_uses_default_ami_when_override_none(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    """When ``ami_id_override`` is None (default), the client's configured AMI flows through."""
    client, stubber = stubbed_client
    stubber.add_response(
        "run_instances",
        {"Instances": [{"InstanceId": "i-default"}]},
        # The client was built with ami_id="ami-test12345"; that's what should be sent.
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
        },
    )
    assert client.create_instance(
        label="mngr-test-aws-host",
        region="us-east-1",
        plan="t3.small",
        user_data="test-user-data",
        ssh_key_ids=[],
        tags={},
    ) == VpsInstanceId("i-default")


def test_create_instance_spot_sets_instance_market_options(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    """``spot=True`` (from --aws-spot) makes RunInstances see InstanceMarketOptions={MarketType: spot}."""
    client, stubber = stubbed_client
    stubber.add_response(
        "run_instances",
        {"Instances": [{"InstanceId": "i-spot"}]},
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
            "InstanceMarketOptions": {"MarketType": "spot"},
        },
    )
    assert client.create_instance(
        label="mngr-test-aws-host",
        region="us-east-1",
        plan="t3.small",
        user_data="test-user-data",
        ssh_key_ids=[],
        tags={},
        spot=True,
    ) == VpsInstanceId("i-spot")


def test_create_instance_no_spot_omits_instance_market_options(
    stubbed_client: tuple[AwsVpsClient, Stubber],
) -> None:
    """``spot=False`` (the default) must NOT emit InstanceMarketOptions; the Stubber asserts on shape.

    The expected_params dict is the *full* set of kwargs sent to RunInstances. If
    AwsVpsClient.create_instance ever started leaking ``InstanceMarketOptions`` into
    the on-demand path, this stub would reject the call as a parameter mismatch.
    """
    client, stubber = stubbed_client
    stubber.add_response(
        "run_instances",
        {"Instances": [{"InstanceId": "i-ondemand"}]},
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
        },
    )
    assert client.create_instance(
        label="mngr-test-aws-host",
        region="us-east-1",
        plan="t3.small",
        user_data="test-user-data",
        ssh_key_ids=[],
        tags={},
    ) == VpsInstanceId("i-ondemand")


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


def test_stop_instance(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    """stop_instance issues StopInstances and waits for the terminal 'stopped' state."""
    client, stubber = stubbed_client
    stubber.add_response(
        "stop_instances",
        {"StoppingInstances": [{"InstanceId": "i-abc"}]},
        expected_params={"InstanceIds": ["i-abc"]},
    )
    stubber.add_response(
        "describe_instances",
        {"Reservations": [{"Instances": [{"InstanceId": "i-abc", "State": {"Name": "stopped"}}]}]},
        expected_params={"InstanceIds": ["i-abc"]},
    )
    client.stop_instance(VpsInstanceId("i-abc"))


def test_stop_instance_times_out_if_not_stopped(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    """A zero timeout means the wait never observes 'stopped' and raises.

    ``wait_for`` makes one final condition check after the (already-expired)
    timeout, so a single ``describe_instances`` (returning the still-stopping
    state) is consumed before the VpsProvisioningError is raised.
    """
    client, stubber = stubbed_client
    stubber.add_response(
        "stop_instances",
        {"StoppingInstances": [{"InstanceId": "i-abc"}]},
        expected_params={"InstanceIds": ["i-abc"]},
    )
    stubber.add_response(
        "describe_instances",
        {"Reservations": [{"Instances": [{"InstanceId": "i-abc", "State": {"Name": "stopping"}}]}]},
        expected_params={"InstanceIds": ["i-abc"]},
    )
    with pytest.raises(VpsProvisioningError, match="did not reach state 'stopped'"):
        client.stop_instance(VpsInstanceId("i-abc"), timeout_seconds=0.0)


def test_start_instance_returns_new_public_ip(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    """start_instance issues StartInstances and returns the (fresh) public IP once running."""
    client, stubber = stubbed_client
    stubber.add_response(
        "start_instances",
        {"StartingInstances": [{"InstanceId": "i-abc"}]},
        expected_params={"InstanceIds": ["i-abc"]},
    )
    # wait_for_instance_active polls get_instance_status (running) then get_instance_ip.
    stubber.add_response(
        "describe_instances",
        {"Reservations": [{"Instances": [{"InstanceId": "i-abc", "State": {"Name": "running"}}]}]},
        expected_params={"InstanceIds": ["i-abc"]},
    )
    stubber.add_response(
        "describe_instances",
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-abc",
                            "State": {"Name": "running"},
                            "PublicIpAddress": "5.6.7.8",
                        }
                    ]
                }
            ]
        },
        expected_params={"InstanceIds": ["i-abc"]},
    )
    assert client.start_instance(VpsInstanceId("i-abc")) == "5.6.7.8"


def test_start_instance_times_out_if_not_active(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    """A zero timeout means the activeness wait never succeeds and raises."""
    client, stubber = stubbed_client
    stubber.add_response(
        "start_instances",
        {"StartingInstances": [{"InstanceId": "i-abc"}]},
        expected_params={"InstanceIds": ["i-abc"]},
    )
    with pytest.raises(VpsProvisioningError, match="did not become active"):
        client.start_instance(VpsInstanceId("i-abc"), timeout_seconds=0.0)


def test_add_tags(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    """add_tags upserts the given tags onto the instance via CreateTags."""
    client, stubber = stubbed_client
    stubber.add_response(
        "create_tags",
        {},
        expected_params={"Resources": ["i-abc"], "Tags": [{"Key": "mngr-agent-x", "Value": "v"}]},
    )
    client.add_tags(VpsInstanceId("i-abc"), {"mngr-agent-x": "v"})


def test_add_tags_empty_is_noop(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    """No tags means no CreateTags call (an unexpected call would make the Stubber raise)."""
    client, _stubber = stubbed_client
    client.add_tags(VpsInstanceId("i-abc"), {})


def test_remove_tags(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    """remove_tags deletes tags by key via DeleteTags (Value omitted)."""
    client, stubber = stubbed_client
    stubber.add_response(
        "delete_tags",
        {},
        expected_params={"Resources": ["i-abc"], "Tags": [{"Key": "mngr-agent-x"}]},
    )
    client.remove_tags(VpsInstanceId("i-abc"), ["mngr-agent-x"])


def test_remove_tags_empty_is_noop(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    """No keys means no DeleteTags call."""
    client, _stubber = stubbed_client
    client.remove_tags(VpsInstanceId("i-abc"), [])


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


def test_ensure_security_group_auto_create_warns_when_no_cidrs(log_warnings: list[str]) -> None:
    """Empty allowed_ssh_cidrs creates/reuses the SG with no ingress and logs a warning.

    Mirrors how Vultr/OVH provisioning behaves in this monorepo (no provider-managed firewall);
    the empty case is a "I'll wire my own ingress later" signal, not a fail-closed gate.

    No ``authorize_security_group_ingress`` calls are issued: real AWS rejects an
    IpPermission with no source set, so the SG keeps its default of zero ingress rules
    (which is exactly the documented "wire it yourself" shape). The absence of
    authorize stubs below is part of the assertion -- the Stubber would raise on any
    unexpected API call.
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
        security_group=AutoCreateSecurityGroup(name="mngr-aws-empty"),
        allowed_ssh_cidrs=(),
        stubbed_ec2_client=ec2,
    )
    stubber.add_response(
        "describe_security_groups",
        {"SecurityGroups": [{"GroupId": "sg-empty", "GroupName": "mngr-aws-empty"}]},
        expected_params={"Filters": [{"Name": "group-name", "Values": ["mngr-aws-empty"]}]},
    )
    stubber.activate()
    try:
        assert client.ensure_security_group() == "sg-empty"
    finally:
        stubber.deactivate()
    assert any("allowed_ssh_cidrs is empty" in msg for msg in log_warnings)


def test_ensure_security_group_auto_create_warns_when_open_to_internet(log_warnings: list[str]) -> None:
    """0.0.0.0/0 is the default but should still produce a visible warning at provision time."""
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
        security_group=AutoCreateSecurityGroup(name="mngr-aws-open"),
        allowed_ssh_cidrs=("0.0.0.0/0",),
        stubbed_ec2_client=ec2,
    )
    stubber.add_response(
        "describe_security_groups",
        {"SecurityGroups": [{"GroupId": "sg-open", "GroupName": "mngr-aws-open"}]},
        expected_params={"Filters": [{"Name": "group-name", "Values": ["mngr-aws-open"]}]},
    )
    stubber.add_response("authorize_security_group_ingress", {})
    stubber.add_response("authorize_security_group_ingress", {})
    stubber.activate()
    try:
        assert client.ensure_security_group() == "sg-open"
    finally:
        stubber.deactivate()
    assert any("0.0.0.0/0" in msg for msg in log_warnings)


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


# =============================================================================
# delete_security_group (inverse of ensure; used by `mngr aws cleanup`)
# =============================================================================


def test_delete_security_group_deletes_when_present(
    auto_sg_client: tuple[AwsVpsClient, Stubber],
) -> None:
    """An auto-created SG is looked up by name and deleted, returning its id."""
    client, stubber = auto_sg_client
    stubber.add_response(
        "describe_security_groups",
        {"SecurityGroups": [{"GroupId": "sg-existing", "GroupName": "mngr-aws-test"}]},
        expected_params={"Filters": [{"Name": "group-name", "Values": ["mngr-aws-test"]}]},
    )
    stubber.add_response("delete_security_group", {}, expected_params={"GroupId": "sg-existing"})
    assert client.delete_security_group() == "sg-existing"


def test_delete_security_group_is_noop_when_missing(
    auto_sg_client: tuple[AwsVpsClient, Stubber],
) -> None:
    """When no SG matches the name, delete is skipped and None is returned (idempotent)."""
    client, stubber = auto_sg_client
    stubber.add_response(
        "describe_security_groups",
        {"SecurityGroups": []},
        expected_params={"Filters": [{"Name": "group-name", "Values": ["mngr-aws-test"]}]},
    )
    assert client.delete_security_group() is None


def test_delete_security_group_refuses_externally_managed_sg(
    stubbed_client: tuple[AwsVpsClient, Stubber],
) -> None:
    """An ``ExistingSecurityGroup`` is user-owned; delete must raise without any API call."""
    client, _stubber = stubbed_client
    with pytest.raises(MngrError, match="externally-managed"):
        client.delete_security_group()


# =============================================================================
# resolve_security_group_id (lookup-only; used by create_instance hot path)
# =============================================================================


def test_resolve_security_group_id_returns_preset_id_when_provided(
    stubbed_client: tuple[AwsVpsClient, Stubber],
) -> None:
    client, _stubber = stubbed_client
    assert client.resolve_security_group_id() == "sg-test"


def test_resolve_security_group_id_returns_existing_sg_id_without_authorizing(
    auto_sg_client: tuple[AwsVpsClient, Stubber],
) -> None:
    """Lookup-only path: must NOT call authorize_security_group_ingress."""
    client, stubber = auto_sg_client
    stubber.add_response(
        "describe_security_groups",
        {"SecurityGroups": [{"GroupId": "sg-existing", "GroupName": "mngr-aws-test"}]},
        expected_params={"Filters": [{"Name": "group-name", "Values": ["mngr-aws-test"]}]},
    )
    # If resolve_security_group_id accidentally calls authorize, the stubber
    # would fail on an unexpected API call -- so the absence of an
    # authorize_security_group_ingress stub here is part of the assertion.
    assert client.resolve_security_group_id() == "sg-existing"


def test_resolve_security_group_id_raises_with_prepare_hint_when_missing(
    auto_sg_client: tuple[AwsVpsClient, Stubber],
) -> None:
    """Missing SG must surface as a clear "run `mngr aws prepare`" error, not a CreateSecurityGroup attempt."""
    client, stubber = auto_sg_client
    stubber.add_response(
        "describe_security_groups",
        {"SecurityGroups": []},
        expected_params={"Filters": [{"Name": "group-name", "Values": ["mngr-aws-test"]}]},
    )
    with pytest.raises(MngrError, match="mngr aws prepare"):
        client.resolve_security_group_id()


def test_resolve_security_group_id_raises_on_multi_vpc_name_collision(
    auto_sg_client: tuple[AwsVpsClient, Stubber],
) -> None:
    """When the same name exists in multiple VPCs, refuse to guess."""
    client, stubber = auto_sg_client
    stubber.add_response(
        "describe_security_groups",
        {
            "SecurityGroups": [
                {"GroupId": "sg-vpc-a", "GroupName": "mngr-aws-test", "VpcId": "vpc-a"},
                {"GroupId": "sg-vpc-b", "GroupName": "mngr-aws-test", "VpcId": "vpc-b"},
            ]
        },
        expected_params={"Filters": [{"Name": "group-name", "Values": ["mngr-aws-test"]}]},
    )
    with pytest.raises(MngrError, match="Found 2 security groups"):
        client.resolve_security_group_id()
