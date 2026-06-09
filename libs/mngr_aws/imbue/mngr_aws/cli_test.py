"""Tests for ``mngr aws`` CLI subcommands.

Splits the test surface into two layers:

- The privileged ``prepare`` path: exercised through ``_build_prepare_client``
  + a real ``_StubbedAwsVpsClient`` rather than mocking the click runtime.
  This keeps the EC2 API contract in the test (botocore Stubber) without
  pulling in ``unittest.mock``.
- Click-level smoke tests: invoke the click commands directly to verify
  exit codes and user-facing error messages on the paths that don't need
  a real EC2 call (the ``ami`` ``[future]`` stub; the no-credentials path).
"""

import boto3
import pytest
from botocore.stub import ANY
from botocore.stub import Stubber
from click.testing import CliRunner

from imbue.mngr_aws.cli import aws_cli_group
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import AutoCreateSecurityGroup
from imbue.mngr_aws.testing import _StubbedAwsVpsClient


def _stubbed_aws_client(
    sg_name: str = "mngr-aws",
    allowed_ssh_cidrs: tuple[str, ...] = ("0.0.0.0/0",),
) -> tuple[AwsVpsClient, Stubber]:
    """Return a ``_StubbedAwsVpsClient`` paired with its botocore Stubber."""
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
        ami_id="ami-placeholder",
        security_group=AutoCreateSecurityGroup(name=sg_name),
        allowed_ssh_cidrs=allowed_ssh_cidrs,
        stubbed_ec2_client=ec2,
    )
    return client, stubber


def test_prepare_logic_creates_sg_when_missing() -> None:
    """The privileged path calls Describe -> Create -> Authorize x2 and returns the new id."""
    client, stubber = _stubbed_aws_client(sg_name="mngr-aws")
    stubber.add_response(
        "describe_security_groups",
        {"SecurityGroups": []},
        expected_params={"Filters": [{"Name": "group-name", "Values": ["mngr-aws"]}]},
    )
    stubber.add_response(
        "create_security_group",
        {"GroupId": "sg-new123"},
        expected_params={"GroupName": "mngr-aws", "Description": ANY},
    )
    stubber.add_response("authorize_security_group_ingress", {})
    stubber.add_response("authorize_security_group_ingress", {})
    stubber.activate()
    try:
        assert client.ensure_security_group() == "sg-new123"
    finally:
        stubber.deactivate()


def test_prepare_logic_reuses_sg_when_present() -> None:
    """When the SG already exists, Describe returns it and Create is skipped."""
    client, stubber = _stubbed_aws_client(sg_name="my-custom-sg")
    stubber.add_response(
        "describe_security_groups",
        {"SecurityGroups": [{"GroupId": "sg-reused", "GroupName": "my-custom-sg"}]},
        expected_params={"Filters": [{"Name": "group-name", "Values": ["my-custom-sg"]}]},
    )
    stubber.add_response("authorize_security_group_ingress", {})
    stubber.add_response("authorize_security_group_ingress", {})
    stubber.activate()
    try:
        assert client.ensure_security_group() == "sg-reused"
    finally:
        stubber.deactivate()


def test_ami_command_is_future_stub() -> None:
    """`mngr aws ami` must explicitly tell the user it's a [future] placeholder."""
    runner = CliRunner()
    result = runner.invoke(aws_cli_group, ["ami"])
    assert result.exit_code != 0
    assert "not yet implemented" in result.output.lower()


def test_prepare_command_fails_clearly_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """When AWS creds aren't resolvable, the click command surfaces a clean error.

    This is the only test that goes through ``CliRunner`` against the real
    ``prepare`` callback, because the credential-missing path is the
    user-facing error message and worth exercising at the CLI boundary.
    """
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    runner = CliRunner()
    result = runner.invoke(aws_cli_group, ["prepare"])
    assert result.exit_code != 0
    assert "credentials not configured" in result.output.lower()


def test_prepare_command_help_is_reachable() -> None:
    """`mngr aws prepare --help` should render without invoking AWS."""
    runner = CliRunner()
    result = runner.invoke(aws_cli_group, ["prepare", "--help"])
    assert result.exit_code == 0
    assert "--region" in result.output
    assert "--sg-name" in result.output
    assert "--allowed-ssh-cidr" in result.output
