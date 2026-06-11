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

import boto3
import click
import pluggy
import pytest
from botocore.stub import ANY
from botocore.stub import Stubber
from click.testing import CliRunner

from imbue.imbue_common.model_update import to_update
from imbue.mngr.config.data_types import MngrContext
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


def test_cleanup_logic_deletes_sg_when_no_instances() -> None:
    """With no mngr instances, cleanup looks up the SG and deletes it, returning its id."""
    client, stubber = _stubbed_aws_client(sg_name="mngr-aws")
    stubber.add_response(
        "describe_instances",
        {"Reservations": []},
        expected_params={
            "Filters": [
                {"Name": "instance-state-name", "Values": _ACTIVE_STATES},
                {"Name": "tag-key", "Values": ["mngr-provider"]},
            ]
        },
    )
    stubber.add_response(
        "describe_security_groups",
        {"SecurityGroups": [{"GroupId": "sg-del123", "GroupName": "mngr-aws"}]},
        expected_params={"Filters": [{"Name": "group-name", "Values": ["mngr-aws"]}]},
    )
    stubber.add_response("delete_security_group", {}, expected_params={"GroupId": "sg-del123"})
    stubber.activate()
    try:
        assert _perform_cleanup(client) == "sg-del123"
    finally:
        stubber.deactivate()


def test_cleanup_logic_is_noop_when_sg_missing() -> None:
    """When the SG is already gone, cleanup deletes nothing and returns None (idempotent)."""
    client, stubber = _stubbed_aws_client(sg_name="mngr-aws")
    stubber.add_response(
        "describe_instances",
        {"Reservations": []},
        expected_params={
            "Filters": [
                {"Name": "instance-state-name", "Values": _ACTIVE_STATES},
                {"Name": "tag-key", "Values": ["mngr-provider"]},
            ]
        },
    )
    stubber.add_response(
        "describe_security_groups",
        {"SecurityGroups": []},
        expected_params={"Filters": [{"Name": "group-name", "Values": ["mngr-aws"]}]},
    )
    stubber.activate()
    try:
        assert _perform_cleanup(client) is None
    finally:
        stubber.deactivate()


def test_cleanup_logic_refuses_when_instances_exist() -> None:
    """A live mngr instance makes cleanup raise without describing or deleting the SG."""
    client, stubber = _stubbed_aws_client(sg_name="mngr-aws")
    stubber.add_response(
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
    stubber.activate()
    try:
        with pytest.raises(click.ClickException) as exc_info:
            _perform_cleanup(client)
        # The refusal must name the blocking instance so the operator knows what to destroy.
        assert "i-abc123" in str(exc_info.value)
        assert "Refusing" in str(exc_info.value)
        # No SG describe/delete was queued, so the only stubbed call was consumed.
        stubber.assert_no_pending_responses()
    finally:
        stubber.deactivate()


def test_cleanup_command_help_is_reachable() -> None:
    """`mngr aws cleanup --help` should render without invoking AWS."""
    runner = CliRunner()
    result = runner.invoke(aws_cli_group, ["cleanup", "--help"])
    assert result.exit_code == 0
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


def test_resolve_provider_config_uses_user_provider_block(
    temp_mngr_ctx: MngrContext,
) -> None:
    user_config = AwsProviderConfig(backend=AWS_BACKEND_NAME, default_region="us-west-2", vpc_id="vpc-user")
    name = ProviderInstanceName("aws-prod")
    new_config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().providers, {name: user_config})
    )
    ctx_with_provider = temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().config, new_config))

    resolved = _resolve_provider_config(ctx_with_provider, "aws-prod")

    assert resolved.default_region == "us-west-2"
    assert resolved.vpc_id == "vpc-user"


def test_resolve_provider_config_falls_back_to_class_defaults_when_missing(
    temp_mngr_ctx: MngrContext,
) -> None:
    """When the named provider block doesn't exist, class defaults are used.

    Operator commands must work for first-run users who haven't yet pinned a
    ``[providers.aws]`` block, so the fallback is a feature not a bug.
    """
    resolved = _resolve_provider_config(temp_mngr_ctx, "aws-does-not-exist")

    assert resolved == AwsProviderConfig()


def test_resolve_provider_config_falls_back_when_named_block_is_non_aws(
    temp_mngr_ctx: MngrContext,
) -> None:
    """If the user pointed ``[providers.aws]`` at a non-AWS backend, fall back.

    The operator CLI still works against the class defaults plus whatever the
    user passes on the command line; refusing here would block a legitimate
    out-of-band run.
    """
    non_aws = LocalProviderConfig()
    name = ProviderInstanceName("aws")
    new_config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().providers, {name: non_aws})
    )
    ctx_with_provider = temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().config, new_config))

    resolved = _resolve_provider_config(ctx_with_provider, "aws")

    assert resolved == AwsProviderConfig()
