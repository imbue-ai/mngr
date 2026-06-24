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

import json

import boto3
import click
import pluggy
import pytest
from botocore.stub import ANY
from botocore.stub import Stubber
from click.testing import CliRunner
from moto import mock_aws

from imbue.imbue_common.model_update import to_update
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.config import LocalProviderConfig
from imbue.mngr_aws.backend import AWS_BACKEND_NAME
from imbue.mngr_aws.cli import _output_cleanup_result
from imbue.mngr_aws.cli import _output_prepare_result
from imbue.mngr_aws.cli import _perform_cleanup
from imbue.mngr_aws.cli import _perform_state_bucket_cleanup
from imbue.mngr_aws.cli import _refuse_cleanup_if_instances_exist
from imbue.mngr_aws.cli import _resolve_provider_config
from imbue.mngr_aws.cli import aws_cli_group
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.client import SecurityGroupPrepareResult
from imbue.mngr_aws.config import AutoCreateSecurityGroup
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_aws.state_bucket import S3StateBucket
from imbue.mngr_aws.testing import _StubbedAwsVpsClient
from imbue.mngr_vps.errors import ManagedResourcesExistError

_ACTIVE_STATES = ["pending", "running", "stopping", "stopped"]


def _stubbed_aws_client(
    sg_name: str = "mngr-aws",
    allowed_ssh_cidrs: tuple[str, ...] = ("0.0.0.0/0",),
) -> tuple[AwsVpsClient, Stubber]:
    """Return a ``_StubbedAwsVpsClient`` paired with its EC2 botocore Stubber.

    ``prepare`` and ``cleanup`` are security-group-only (no IAM), so the helper
    wires a single Stubber around the EC2 client.
    """
    session = boto3.Session(
        aws_access_key_id="AKIATEST",
        aws_secret_access_key="secret",
        region_name="us-east-1",
    )
    ec2 = session.client("ec2", region_name="us-east-1")
    ec2_stubber = Stubber(ec2)
    client = _StubbedAwsVpsClient(
        session=session,
        region="us-east-1",
        ami_id="ami-placeholder",
        security_group=AutoCreateSecurityGroup(name=sg_name),
        allowed_ssh_cidrs=allowed_ssh_cidrs,
        stubbed_ec2_client=ec2,
    )
    return client, ec2_stubber


def test_prepare_logic_creates_sg_when_missing() -> None:
    """The privileged path creates the SG (Describe -> Create -> Authorize x2)."""
    client, ec2_stubber = _stubbed_aws_client(sg_name="mngr-aws")
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
    ec2_stubber.activate()
    try:
        result = client.ensure_security_group()
        ec2_stubber.assert_no_pending_responses()
    finally:
        ec2_stubber.deactivate()
    assert result.security_group_id == "sg-new123"
    assert result.was_created is True


def test_prepare_logic_reuses_sg_when_present() -> None:
    """When the SG already exists, Describe returns it and Create is skipped."""
    client, ec2_stubber = _stubbed_aws_client(sg_name="my-custom-sg")
    ec2_stubber.add_response(
        "describe_security_groups",
        {"SecurityGroups": [{"GroupId": "sg-reused", "GroupName": "my-custom-sg"}]},
        expected_params={"Filters": [{"Name": "group-name", "Values": ["my-custom-sg"]}]},
    )
    ec2_stubber.add_response("authorize_security_group_ingress", {})
    ec2_stubber.add_response("authorize_security_group_ingress", {})
    ec2_stubber.activate()
    try:
        result = client.ensure_security_group()
    finally:
        ec2_stubber.deactivate()
    assert result.security_group_id == "sg-reused"
    assert result.was_created is False


def test_cleanup_logic_deletes_sg_when_no_instances() -> None:
    """With no mngr instances, cleanup deletes the SG and returns its id."""
    client, ec2_stubber = _stubbed_aws_client(sg_name="mngr-aws")
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
    ec2_stubber.activate()
    try:
        assert _perform_cleanup(client) == "sg-del123"
        ec2_stubber.assert_no_pending_responses()
    finally:
        ec2_stubber.deactivate()


def test_cleanup_logic_is_noop_when_sg_missing() -> None:
    """When the SG is already gone, cleanup deletes nothing and returns None."""
    client, ec2_stubber = _stubbed_aws_client(sg_name="mngr-aws")
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
    ec2_stubber.activate()
    try:
        assert _perform_cleanup(client) is None
        ec2_stubber.assert_no_pending_responses()
    finally:
        ec2_stubber.deactivate()


def test_cleanup_logic_refuses_when_instances_exist() -> None:
    """A live mngr instance makes cleanup raise without describing/deleting the SG."""
    client, ec2_stubber = _stubbed_aws_client(sg_name="mngr-aws")
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
    try:
        with pytest.raises(ManagedResourcesExistError) as exc_info:
            _perform_cleanup(client)
        # The refusal must name the blocking instance so the operator knows what to destroy.
        assert "i-abc123" in str(exc_info.value)
        assert "Refusing" in str(exc_info.value)
        # No SG delete was queued, so the only stubbed call was the instance describe.
        ec2_stubber.assert_no_pending_responses()
    finally:
        ec2_stubber.deactivate()


def test_refuse_cleanup_if_instances_exist_aborts_before_teardown() -> None:
    """The instance-exists refusal runs first, so the bucket/identity are never torn down.

    Reproduces the callback ordering: when an instance is still alive, the guard
    raises before any bucket teardown, so a bucket holding host state survives.
    """
    client, ec2_stubber = _stubbed_aws_client(sg_name="mngr-aws")
    ec2_stubber.add_response(
        "describe_instances",
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-live999",
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
    with mock_aws():
        session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1")
        bucket = S3StateBucket(session=session, region="us-east-1", bucket_name="mngr-state-refuse-first")
        bucket.ensure_bucket()
        bucket.write_host_record_json(HostId.generate(), "{}")
        ec2_stubber.activate()
        try:
            with pytest.raises(ManagedResourcesExistError, match="Refusing"):
                _refuse_cleanup_if_instances_exist(client)
            ec2_stubber.assert_no_pending_responses()
        finally:
            ec2_stubber.deactivate()
        # The guard raised before any teardown, so the bucket and its state survive.
        assert bucket.bucket_exists() is True
        assert bucket.has_any_host_state() is True


def test_perform_state_bucket_cleanup_refuses_while_host_state_remains() -> None:
    """The bucket cleanup refuses (deletes nothing) while any host state remains."""
    with mock_aws():
        session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1")
        bucket = S3StateBucket(session=session, region="us-east-1", bucket_name="mngr-state-cleanup-refuse")
        bucket.ensure_bucket()
        bucket.write_host_record_json(HostId.generate(), "{}")
        with pytest.raises(click.ClickException, match="still holds offline host state"):
            _perform_state_bucket_cleanup(bucket, force=False)
        # The bucket must still exist after a refusal.
        assert bucket.bucket_exists() is True


def test_perform_state_bucket_cleanup_force_deletes_despite_host_state() -> None:
    """``--force`` deletes the bucket (and its leftover state) instead of refusing."""
    with mock_aws():
        session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1")
        bucket = S3StateBucket(session=session, region="us-east-1", bucket_name="mngr-state-cleanup-purge")
        bucket.ensure_bucket()
        bucket.write_host_record_json(HostId.generate(), "{}")
        assert _perform_state_bucket_cleanup(bucket, force=True) == "mngr-state-cleanup-purge"
        assert bucket.bucket_exists() is False


def test_perform_state_bucket_cleanup_deletes_empty_bucket() -> None:
    """With no host state, the bucket cleanup deletes the bucket and returns its name."""
    with mock_aws():
        session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1")
        bucket = S3StateBucket(session=session, region="us-east-1", bucket_name="mngr-state-cleanup-empty")
        bucket.ensure_bucket()
        assert _perform_state_bucket_cleanup(bucket, force=False) == "mngr-state-cleanup-empty"
        assert bucket.bucket_exists() is False


def test_perform_state_bucket_cleanup_none_is_noop() -> None:
    """A None bucket (none configured) is a harmless no-op."""
    assert _perform_state_bucket_cleanup(None, force=False) is None


# =============================================================================
# format-aware output (prepare / cleanup respect --format)
# =============================================================================


def test_output_prepare_result_human_emits_single_line(capsys: pytest.CaptureFixture[str]) -> None:
    """HUMAN mode emits one SG sentence (plus a bucket line when a bucket was set up)."""
    result = SecurityGroupPrepareResult(security_group_id="sg-new123", was_created=True)
    # No bucket name passed: the output helper emits only the SG line.
    _output_prepare_result(result, "us-east-1", None, False, OutputFormat.HUMAN)
    captured = capsys.readouterr()
    assert captured.out == "Prepared AWS security group sg-new123 in region us-east-1\n"


def test_output_prepare_result_human_includes_bucket_line(capsys: pytest.CaptureFixture[str]) -> None:
    """HUMAN mode appends a bucket line when a state bucket was created."""
    result = SecurityGroupPrepareResult(security_group_id="sg-new123", was_created=True)
    _output_prepare_result(result, "us-east-1", "mngr-state-123-us-east-1", True, OutputFormat.HUMAN)
    captured = capsys.readouterr()
    assert "Prepared AWS security group sg-new123 in region us-east-1\n" in captured.out
    assert "Created S3 state bucket mngr-state-123-us-east-1 in region us-east-1\n" in captured.out


def test_output_prepare_result_json_carries_created_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON mode emits a structured object including the created signal and bucket fields."""
    result = SecurityGroupPrepareResult(security_group_id="sg-reused", was_created=False)
    _output_prepare_result(result, "us-east-1", "mngr-state-123-us-east-1", False, OutputFormat.JSON)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {
        "security_group_id": "sg-reused",
        "region": "us-east-1",
        "created": False,
        "state_bucket_name": "mngr-state-123-us-east-1",
        "state_bucket_created": False,
    }


def test_output_prepare_result_jsonl_emits_prepared_event(capsys: pytest.CaptureFixture[str]) -> None:
    """JSONL mode emits a ``prepared`` event with the same fields."""
    result = SecurityGroupPrepareResult(security_group_id="sg-new123", was_created=True)
    _output_prepare_result(result, "us-east-1", None, False, OutputFormat.JSONL)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["event"] == "prepared"
    assert payload["created"] is True
    assert payload["security_group_id"] == "sg-new123"
    assert payload["state_bucket_name"] is None


def test_output_cleanup_result_json_reports_deleted(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON cleanup output reports deleted=True when an SG was removed."""
    _output_cleanup_result("sg-gone", "us-east-1", "mngr-state-123-us-east-1", OutputFormat.JSON)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {
        "security_group_id": "sg-gone",
        "region": "us-east-1",
        "deleted": True,
        "state_bucket_deleted": "mngr-state-123-us-east-1",
    }


def test_output_cleanup_result_json_reports_noop(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON cleanup output reports deleted=False on the idempotent no-op path."""
    _output_cleanup_result(None, "us-east-1", None, OutputFormat.JSON)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {
        "security_group_id": None,
        "region": "us-east-1",
        "deleted": False,
        "state_bucket_deleted": None,
    }


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
    log_warnings: list[str],
) -> None:
    """The happy path returns the configured ``AwsProviderConfig`` verbatim, silently.

    Pins the third leg of the three-case contract: configured AWS block ->
    return as-is, no warning. The two sibling tests cover the missing-block
    and non-AWS-block fallbacks (silent and warning respectively); pinning
    silence here too closes the {AWS / non-AWS / missing} x {warn / silent}
    matrix so a future regression that always-warns can't slip through.
    """
    user_config = AwsProviderConfig(backend=AWS_BACKEND_NAME, default_region="us-west-2", vpc_id="vpc-user")
    ctx_with_provider = _temp_mngr_ctx_with_provider(temp_mngr_ctx, "aws-prod", user_config)

    resolved = _resolve_provider_config(ctx_with_provider, "aws-prod")

    assert resolved.default_region == "us-west-2"
    assert resolved.vpc_id == "vpc-user"
    assert log_warnings == [], f"happy path must be silent, got {log_warnings!r}"


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
