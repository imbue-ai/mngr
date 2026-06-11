"""Tests for ``mngr aws`` CLI subcommands.

Splits the test surface into two layers:

- The SG-management logic that the ``prepare`` callback invokes:
  exercised against ``_StubbedAwsVpsClient`` with a botocore ``Stubber``
  queuing the expected EC2 API calls. Bypasses the click runtime so the
  wire contract (Describe -> Create -> Authorize x2) can be asserted on
  directly, without ``unittest.mock``.
- Click-level smoke tests: invoke the click commands through
  ``CliRunner`` to verify exit codes and user-facing error messages on
  the paths that don't need a real EC2 call (the ``ami`` ``[future]``
  stub; the no-credentials path; ``prepare --help``).
"""

from datetime import datetime
from datetime import timezone

import boto3
import click
import pluggy
import pytest
from botocore.stub import ANY
from botocore.stub import Stubber
from click.testing import CliRunner

from imbue.imbue_common.model_update import to_update
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.config import LocalProviderConfig
from imbue.mngr_aws.backend import AWS_BACKEND_NAME
from imbue.mngr_aws.cli import _perform_cleanup
from imbue.mngr_aws.cli import _resolve_provider_config
from imbue.mngr_aws.cli import aws_cli_group
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import AutoCreateSecurityGroup
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_aws.testing import _StubbedAwsVpsClient

_ACTIVE_STATES = ["pending", "running", "stopping", "stopped"]


def _stubbed_aws_client(
    sg_name: str = "mngr-aws",
    allowed_ssh_cidrs: tuple[str, ...] = ("0.0.0.0/0",),
) -> tuple[AwsVpsClient, Stubber, Stubber]:
    """Return a ``_StubbedAwsVpsClient`` paired with its EC2 and IAM botocore Stubbers.

    ``prepare`` and ``cleanup`` touch both EC2 (security group) and IAM
    (self-stop instance profile), so the helper wires a Stubber around each.
    """
    session = boto3.Session(
        aws_access_key_id="AKIATEST",
        aws_secret_access_key="secret",
        region_name="us-east-1",
    )
    ec2 = session.client("ec2", region_name="us-east-1")
    iam = session.client("iam")
    ec2_stubber = Stubber(ec2)
    iam_stubber = Stubber(iam)
    client = _StubbedAwsVpsClient(
        session=session,
        region="us-east-1",
        ami_id="ami-placeholder",
        security_group=AutoCreateSecurityGroup(name=sg_name),
        allowed_ssh_cidrs=allowed_ssh_cidrs,
        stubbed_ec2_client=ec2,
        stubbed_iam_client=iam,
    )
    return client, ec2_stubber, iam_stubber


def _queue_ensure_instance_profile(iam_stubber: Stubber) -> None:
    """Queue the four IAM responses for a from-scratch ``ensure_self_stop_instance_profile``."""
    iam_stubber.add_response(
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
    iam_stubber.add_response(
        "put_role_policy",
        {},
        expected_params={"RoleName": "mngr-aws", "PolicyName": "mngr-aws", "PolicyDocument": ANY},
    )
    iam_stubber.add_response(
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
    iam_stubber.add_response(
        "add_role_to_instance_profile",
        {},
        expected_params={"InstanceProfileName": "mngr-aws", "RoleName": "mngr-aws"},
    )


def _queue_delete_instance_profile(iam_stubber: Stubber) -> None:
    """Queue the four IAM responses for a full ``delete_self_stop_instance_profile``."""
    iam_stubber.add_response(
        "remove_role_from_instance_profile",
        {},
        expected_params={"InstanceProfileName": "mngr-aws", "RoleName": "mngr-aws"},
    )
    iam_stubber.add_response("delete_instance_profile", {}, expected_params={"InstanceProfileName": "mngr-aws"})
    iam_stubber.add_response(
        "delete_role_policy", {}, expected_params={"RoleName": "mngr-aws", "PolicyName": "mngr-aws"}
    )
    iam_stubber.add_response("delete_role", {}, expected_params={"RoleName": "mngr-aws"})


def test_prepare_logic_creates_sg_and_instance_profile_when_missing() -> None:
    """The privileged path creates the SG (Describe -> Create -> Authorize x2) and the IAM profile."""
    client, ec2_stubber, iam_stubber = _stubbed_aws_client(sg_name="mngr-aws")
    ec2_stubber.add_response(
        "describe_security_groups",
        {"SecurityGroups": []},
        expected_params={"Filters": [{"Name": "group-name", "Values": ["mngr-aws"]}]},
    )
    ec2_stubber.add_response(
        "create_security_group",
        {"GroupId": "sg-new123"},
        expected_params={"GroupName": "mngr-aws", "Description": ANY},
    )
    ec2_stubber.add_response("authorize_security_group_ingress", {})
    ec2_stubber.add_response("authorize_security_group_ingress", {})
    _queue_ensure_instance_profile(iam_stubber)
    ec2_stubber.activate()
    iam_stubber.activate()
    try:
        assert client.ensure_security_group() == "sg-new123"
        assert client.ensure_self_stop_instance_profile() == "mngr-aws"
        iam_stubber.assert_no_pending_responses()
    finally:
        iam_stubber.deactivate()
        ec2_stubber.deactivate()


def test_prepare_logic_reuses_sg_when_present() -> None:
    """When the SG already exists, Describe returns it and Create is skipped."""
    client, ec2_stubber, _iam_stubber = _stubbed_aws_client(sg_name="my-custom-sg")
    ec2_stubber.add_response(
        "describe_security_groups",
        {"SecurityGroups": [{"GroupId": "sg-reused", "GroupName": "my-custom-sg"}]},
        expected_params={"Filters": [{"Name": "group-name", "Values": ["my-custom-sg"]}]},
    )
    ec2_stubber.add_response("authorize_security_group_ingress", {})
    ec2_stubber.add_response("authorize_security_group_ingress", {})
    ec2_stubber.activate()
    try:
        assert client.ensure_security_group() == "sg-reused"
    finally:
        ec2_stubber.deactivate()


def test_cleanup_logic_deletes_sg_and_instance_profile_when_no_instances() -> None:
    """With no mngr instances, cleanup deletes the SG and the IAM profile, returning both."""
    client, ec2_stubber, iam_stubber = _stubbed_aws_client(sg_name="mngr-aws")
    ec2_stubber.add_response(
        "describe_instances",
        {"Reservations": []},
        expected_params={
            "Filters": [
                {"Name": "instance-state-name", "Values": _ACTIVE_STATES},
                {"Name": "tag-key", "Values": ["mngr-provider"]},
            ]
        },
    )
    ec2_stubber.add_response(
        "describe_security_groups",
        {"SecurityGroups": [{"GroupId": "sg-del123", "GroupName": "mngr-aws"}]},
        expected_params={"Filters": [{"Name": "group-name", "Values": ["mngr-aws"]}]},
    )
    ec2_stubber.add_response("delete_security_group", {}, expected_params={"GroupId": "sg-del123"})
    _queue_delete_instance_profile(iam_stubber)
    ec2_stubber.activate()
    iam_stubber.activate()
    try:
        assert _perform_cleanup(client) == ("sg-del123", "mngr-aws")
        iam_stubber.assert_no_pending_responses()
    finally:
        iam_stubber.deactivate()
        ec2_stubber.deactivate()


def test_cleanup_logic_is_noop_when_sg_and_profile_missing() -> None:
    """When the SG and IAM profile are already gone, cleanup deletes nothing and returns (None, None)."""
    client, ec2_stubber, iam_stubber = _stubbed_aws_client(sg_name="mngr-aws")
    ec2_stubber.add_response(
        "describe_instances",
        {"Reservations": []},
        expected_params={
            "Filters": [
                {"Name": "instance-state-name", "Values": _ACTIVE_STATES},
                {"Name": "tag-key", "Values": ["mngr-provider"]},
            ]
        },
    )
    ec2_stubber.add_response(
        "describe_security_groups",
        {"SecurityGroups": []},
        expected_params={"Filters": [{"Name": "group-name", "Values": ["mngr-aws"]}]},
    )
    for operation, params in (
        ("remove_role_from_instance_profile", {"InstanceProfileName": "mngr-aws", "RoleName": "mngr-aws"}),
        ("delete_instance_profile", {"InstanceProfileName": "mngr-aws"}),
        ("delete_role_policy", {"RoleName": "mngr-aws", "PolicyName": "mngr-aws"}),
        ("delete_role", {"RoleName": "mngr-aws"}),
    ):
        iam_stubber.add_client_error(
            operation,
            service_error_code="NoSuchEntity",
            service_message="The entity does not exist.",
            http_status_code=404,
            expected_params=params,
        )
    ec2_stubber.activate()
    iam_stubber.activate()
    try:
        assert _perform_cleanup(client) == (None, None)
        iam_stubber.assert_no_pending_responses()
    finally:
        iam_stubber.deactivate()
        ec2_stubber.deactivate()


def test_cleanup_logic_refuses_when_instances_exist() -> None:
    """A live mngr instance makes cleanup raise without describing/deleting the SG or IAM profile."""
    client, ec2_stubber, iam_stubber = _stubbed_aws_client(sg_name="mngr-aws")
    ec2_stubber.add_response(
        "describe_instances",
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-abc123",
                            "State": {"Name": "running"},
                            "Tags": [{"Key": "mngr-provider", "Value": "aws"}],
                        }
                    ]
                }
            ]
        },
        expected_params={
            "Filters": [
                {"Name": "instance-state-name", "Values": _ACTIVE_STATES},
                {"Name": "tag-key", "Values": ["mngr-provider"]},
            ]
        },
    )
    ec2_stubber.activate()
    iam_stubber.activate()
    try:
        with pytest.raises(click.ClickException) as exc_info:
            _perform_cleanup(client)
        # The refusal must name the blocking instance so the operator knows what to destroy.
        assert "i-abc123" in str(exc_info.value)
        assert "Refusing" in str(exc_info.value)
        # No SG or IAM delete was queued, so the only stubbed call was the instance describe.
        ec2_stubber.assert_no_pending_responses()
        iam_stubber.assert_no_pending_responses()
    finally:
        iam_stubber.deactivate()
        ec2_stubber.deactivate()


def test_cleanup_command_help_is_reachable() -> None:
    """`mngr aws cleanup --help` should render without invoking AWS."""
    runner = CliRunner()
    result = runner.invoke(aws_cli_group, ["cleanup", "--help"])
    assert result.exit_code == 0
    assert "--provider" in result.output
    assert "--region" in result.output
    assert "--sg-name" in result.output


def test_ami_command_is_future_stub() -> None:
    """`mngr aws ami` must explicitly tell the user it's a [future] placeholder."""
    runner = CliRunner()
    result = runner.invoke(aws_cli_group, ["ami"])
    assert result.exit_code != 0
    assert "not yet implemented" in result.output.lower()


def test_prepare_command_fails_clearly_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """When AWS creds aren't resolvable, the click command surfaces a clean error.

    This is the only test that goes through ``CliRunner`` against the real
    ``prepare`` callback, because the credential-missing path is the
    user-facing error message and worth exercising at the CLI boundary.
    Passes ``obj=plugin_manager`` because ``prepare`` now runs through
    ``setup_command_context`` (so it can read ``[providers.NAME]`` from the
    user's settings.toml as defaults), and ``setup_command_context`` reads
    the plugin manager off ``ctx.obj``.
    """
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    result = cli_runner.invoke(aws_cli_group, ["prepare"], obj=plugin_manager)
    assert result.exit_code != 0
    assert "credentials not configured" in result.output.lower()


def test_prepare_command_help_is_reachable() -> None:
    """`mngr aws prepare --help` should render without invoking AWS."""
    runner = CliRunner()
    result = runner.invoke(aws_cli_group, ["prepare", "--help"])
    assert result.exit_code == 0
    assert "--provider" in result.output
    assert "--region" in result.output
    assert "--sg-name" in result.output
    assert "--allowed-ssh-cidr" in result.output


# =============================================================================
# Provider-config resolution (the bug-fix surface for `mngr aws prepare`)
# =============================================================================
#
# Earlier versions of ``_build_operator_client`` used ``AwsProviderConfig()``
# class defaults unconditionally, so a user with a non-default ``default_region``
# in ``[providers.aws]`` running ``mngr aws prepare`` without ``--region``
# would land the SG in ``us-east-1`` while the runtime create path looked in
# whatever region their settings.toml specified. ``_resolve_provider_config``
# fixes this by reading the user's resolved provider config off the
# ``MngrContext``; these tests pin that behavior.


def _temp_mngr_ctx_with_provider(temp_mngr_ctx: MngrContext, name: str, config: ProviderInstanceConfig) -> MngrContext:
    """Return ``temp_mngr_ctx`` with ``config`` registered under ``name`` in ``providers``."""
    provider_name = ProviderInstanceName(name)
    new_config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().providers, {provider_name: config})
    )
    return temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().config, new_config))


def test_resolve_provider_config_uses_user_provider_block(
    temp_mngr_ctx: MngrContext,
) -> None:
    user_config = AwsProviderConfig(backend=AWS_BACKEND_NAME, default_region="us-west-2", vpc_id="vpc-user")
    ctx_with_provider = _temp_mngr_ctx_with_provider(temp_mngr_ctx, "aws-prod", user_config)

    resolved = _resolve_provider_config(ctx_with_provider, "aws-prod")

    assert resolved.default_region == "us-west-2"
    assert resolved.vpc_id == "vpc-user"


def test_resolve_provider_config_falls_back_to_class_defaults_when_missing(
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    """When the named provider block doesn't exist, class defaults are used silently.

    Operator commands must work for first-run users who haven't yet pinned a
    ``[providers.aws]`` block, so the fallback is a feature not a bug -- and
    no warning is emitted because this is the expected shape (distinct from
    the wrong-type case, which does warn).
    """
    resolved = _resolve_provider_config(temp_mngr_ctx, "aws-does-not-exist")

    assert resolved == AwsProviderConfig()
    assert log_warnings == [], f"missing-block fallback must be silent, got {log_warnings!r}"


def test_resolve_provider_config_falls_back_when_named_block_is_non_aws(
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    """If the user pointed ``[providers.aws]`` at a non-AWS backend, fall back and warn.

    The operator CLI still works against the class defaults plus whatever the
    user passes on the command line; refusing here would block a legitimate
    out-of-band run. But the user's ``--provider`` selection did not have the
    intended effect, so a warning is emitted to make the silent-fallback
    visible (distinct from the missing-block case, which is silent because it
    is the expected first-run shape).
    """
    ctx_with_provider = _temp_mngr_ctx_with_provider(temp_mngr_ctx, "aws", LocalProviderConfig())

    resolved = _resolve_provider_config(ctx_with_provider, "aws")

    assert resolved == AwsProviderConfig()
    assert len(log_warnings) == 1, f"expected exactly one warning, got {log_warnings!r}"
    assert "'aws'" in log_warnings[0]
    assert "LocalProviderConfig" in log_warnings[0]
