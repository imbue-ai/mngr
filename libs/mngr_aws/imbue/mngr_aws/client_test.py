"""Tests for the AWS EC2 client.

We use ``botocore.stub.Stubber`` to script EC2 API responses without making
real API calls. The fixture below creates a real boto3 session and EC2 client
and wraps it in a stubber so each test can declaratively queue expected
requests and canned responses.
"""

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import ANY
from botocore.stub import Stubber

from imbue.mngr.errors import MngrError
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import AutoCreateSecurityGroup
from imbue.mngr_aws.config import ExistingSecurityGroup
from imbue.mngr_aws.testing import _StubbedAwsVpsClient
from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vps.errors import VpsProvisioningError
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import VpsInstanceStatus


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


def _make_stubbed_client(**client_kwargs: Any) -> tuple[AwsVpsClient, Stubber]:
    """Build a ``_StubbedAwsVpsClient`` + (inactive) ``Stubber``, overriding client kwargs.

    Shared spine for the ``create_instance`` variants that need a non-default
    client field (``terminate_on_shutdown`` / ``iam_instance_profile``). Unlike
    the ``stubbed_client`` fixture it does not activate the stubber: the caller
    queues its ``run_instances`` stub and activates so the per-test
    ``expected_params`` stay explicit.
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
        **client_kwargs,
    )
    return client, stubber


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
            # Default terminate_on_shutdown is False, so an OS shutdown STOPS
            # (resumable idle-pause) rather than terminating the instance.
            "InstanceInitiatedShutdownBehavior": "stop",
            "NetworkInterfaces": ANY,
            "MetadataOptions": ANY,
            "TagSpecifications": ANY,
            "KeyName": "key-1",
            # No default IAM instance profile: idle self-stop powers the host
            # off (no EC2 API call), so no profile is attached unless the
            # operator sets iam_instance_profile explicitly.
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
            "InstanceInitiatedShutdownBehavior": "stop",
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
            "InstanceInitiatedShutdownBehavior": "stop",
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
            "InstanceInitiatedShutdownBehavior": "stop",
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
            "InstanceInitiatedShutdownBehavior": "stop",
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


def test_create_instance_terminate_on_shutdown_sets_terminate_behavior() -> None:
    """terminate_on_shutdown=True flips InstanceInitiatedShutdownBehavior to 'terminate'."""
    client, stubber = _make_stubbed_client(terminate_on_shutdown=True)
    stubber.add_response(
        "run_instances",
        {"Instances": [{"InstanceId": "i-ephemeral"}]},
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
    stubber.activate()
    try:
        assert client.create_instance(
            label="mngr-test-aws-host",
            region="us-east-1",
            plan="t3.small",
            user_data="test-user-data",
            ssh_key_ids=[],
            tags={},
        ) == VpsInstanceId("i-ephemeral")
    finally:
        stubber.deactivate()


def test_create_instance_attaches_explicit_iam_instance_profile() -> None:
    """An explicit iam_instance_profile is attached as IamInstanceProfile={"Name": ...}."""
    client, stubber = _make_stubbed_client(iam_instance_profile="custom-profile")
    stubber.add_response(
        "run_instances",
        {"Instances": [{"InstanceId": "i-profile"}]},
        expected_params={
            "ImageId": "ami-test12345",
            "InstanceType": "t3.small",
            "MinCount": 1,
            "MaxCount": 1,
            "UserData": "test-user-data",
            "BlockDeviceMappings": ANY,
            "NetworkInterfaces": ANY,
            "InstanceInitiatedShutdownBehavior": "stop",
            "MetadataOptions": ANY,
            "TagSpecifications": ANY,
            "IamInstanceProfile": {"Name": "custom-profile"},
        },
    )
    stubber.activate()
    try:
        assert client.create_instance(
            label="mngr-test-aws-host",
            region="us-east-1",
            plan="t3.small",
            user_data="test-user-data",
            ssh_key_ids=[],
            tags={},
        ) == VpsInstanceId("i-profile")
    finally:
        stubber.deactivate()


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


def test_set_instance_tags_upserts_via_create_tags(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    """set_instance_tags issues CreateTags (an upsert) with the given Resources + Tags.

    This backs the rename re-stamp of the EC2 ``Name`` identity tag offline
    discovery reads.
    """
    client, stubber = stubbed_client
    stubber.add_response(
        "create_tags",
        {},
        expected_params={
            "Resources": ["i-abc"],
            "Tags": [{"Key": "Name", "Value": "mngr-renamed"}],
        },
    )
    client.set_instance_tags(VpsInstanceId("i-abc"), {"Name": "mngr-renamed"})
    stubber.assert_no_pending_responses()


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
    # start_instance first checks the state (to wait out a `stopping` instance);
    # here it is already stopped, so it proceeds straight to StartInstances.
    stubber.add_response(
        "describe_instances",
        {"Reservations": [{"Instances": [{"InstanceId": "i-abc", "State": {"Name": "stopped"}}]}]},
        expected_params={"InstanceIds": ["i-abc"]},
    )
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
        "describe_instances",
        {"Reservations": [{"Instances": [{"InstanceId": "i-abc", "State": {"Name": "stopped"}}]}]},
        expected_params={"InstanceIds": ["i-abc"]},
    )
    stubber.add_response(
        "start_instances",
        {"StartingInstances": [{"InstanceId": "i-abc"}]},
        expected_params={"InstanceIds": ["i-abc"]},
    )
    with pytest.raises(VpsProvisioningError, match="did not become active"):
        client.start_instance(VpsInstanceId("i-abc"), timeout_seconds=0.0)


def test_start_instance_waits_for_stopped_when_still_stopping(stubbed_client: tuple[AwsVpsClient, Stubber]) -> None:
    """A still-``stopping`` instance is waited out to ``stopped`` before StartInstances is issued.

    AWS rejects start-instances on a stopping instance (IncorrectInstanceState), so
    resuming a host caught mid-stop (e.g. just powered off by the idle watcher) must
    first wait for the terminal stopped state.
    """
    client, stubber = stubbed_client
    # Initial state check: still stopping.
    stubber.add_response(
        "describe_instances",
        {"Reservations": [{"Instances": [{"InstanceId": "i-abc", "State": {"Name": "stopping"}}]}]},
        expected_params={"InstanceIds": ["i-abc"]},
    )
    # _wait_for_instance_state polls until stopped.
    stubber.add_response(
        "describe_instances",
        {"Reservations": [{"Instances": [{"InstanceId": "i-abc", "State": {"Name": "stopped"}}]}]},
        expected_params={"InstanceIds": ["i-abc"]},
    )
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
                {"Instances": [{"InstanceId": "i-abc", "State": {"Name": "running"}, "PublicIpAddress": "5.6.7.8"}]}
            ]
        },
        expected_params={"InstanceIds": ["i-abc"]},
    )
    assert client.start_instance(VpsInstanceId("i-abc")) == "5.6.7.8"


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


# =============================================================================
# ensure_security_group
# =============================================================================


def test_ensure_security_group_returns_preset_id_when_provided(
    stubbed_client: tuple[AwsVpsClient, Stubber],
) -> None:
    client, _stubber = stubbed_client
    result = client.ensure_security_group()
    assert result.security_group_id == "sg-test"
    # A caller-supplied ExistingSecurityGroup is never created by prepare.
    assert result.was_created is False


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
        result = client.ensure_security_group()
    finally:
        stubber.deactivate()
    assert result.security_group_id == "sg-empty"
    assert result.was_created is False
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
        result = client.ensure_security_group()
    finally:
        stubber.deactivate()
    assert result.security_group_id == "sg-open"
    assert result.was_created is False
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
    result = client.ensure_security_group()
    assert result.security_group_id == "sg-existing"
    assert result.was_created is False


def test_ensure_security_group_skips_authorize_when_ingress_already_present(
    auto_sg_client: tuple[AwsVpsClient, Stubber],
) -> None:
    """Read-only-first: when the SG already permits the required ingress, no write call is issued.

    This is the path minds' auto-prepare relies on with a describe-only AWS key:
    an already-prepared region must succeed without ``AuthorizeSecurityGroupIngress``.
    The absence of any authorize stub below is the assertion -- the Stubber raises
    on any unexpected API call. The describe response carries both required ports
    (tcp/22 and tcp/2222) already open to the configured CIDR.
    """
    client, stubber = auto_sg_client
    stubber.add_response(
        "describe_security_groups",
        {
            "SecurityGroups": [
                {
                    "GroupId": "sg-ready",
                    "GroupName": "mngr-aws-test",
                    "IpPermissions": [
                        {
                            "IpProtocol": "tcp",
                            "FromPort": 22,
                            "ToPort": 22,
                            "IpRanges": [{"CidrIp": "203.0.113.4/32"}],
                        },
                        {
                            "IpProtocol": "tcp",
                            "FromPort": 2222,
                            "ToPort": 2222,
                            "IpRanges": [{"CidrIp": "203.0.113.4/32"}],
                        },
                    ],
                }
            ]
        },
        expected_params={"Filters": [{"Name": "group-name", "Values": ["mngr-aws-test"]}]},
    )
    result = client.ensure_security_group()
    assert result.security_group_id == "sg-ready"
    assert result.was_created is False


def test_ensure_security_group_authorizes_when_one_port_missing(
    auto_sg_client: tuple[AwsVpsClient, Stubber],
) -> None:
    """When the SG exists but a required port is not yet open, fall through to authorize.

    tcp/22 is already present; tcp/2222 is missing, so the read-only-first check
    must NOT short-circuit -- both ports get re-authorized idempotently.
    """
    client, stubber = auto_sg_client
    stubber.add_response(
        "describe_security_groups",
        {
            "SecurityGroups": [
                {
                    "GroupId": "sg-partial",
                    "GroupName": "mngr-aws-test",
                    "IpPermissions": [
                        {
                            "IpProtocol": "tcp",
                            "FromPort": 22,
                            "ToPort": 22,
                            "IpRanges": [{"CidrIp": "203.0.113.4/32"}],
                        },
                    ],
                }
            ]
        },
        expected_params={"Filters": [{"Name": "group-name", "Values": ["mngr-aws-test"]}]},
    )
    stubber.add_response(
        "authorize_security_group_ingress",
        {},
        expected_params={"GroupId": "sg-partial", "IpPermissions": ANY},
    )
    stubber.add_response(
        "authorize_security_group_ingress",
        {},
        expected_params={"GroupId": "sg-partial", "IpPermissions": ANY},
    )
    result = client.ensure_security_group()
    assert result.security_group_id == "sg-partial"
    assert result.was_created is False


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
    result = client.ensure_security_group()
    assert result.security_group_id == "sg-new"
    assert result.was_created is True


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
    result = client.ensure_security_group()
    assert result.security_group_id == "sg-existing"
    assert result.was_created is False


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
