"""Tests for AWS provider backend registration."""

import boto3
import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_aws.backend import AWS_BACKEND_NAME
from imbue.mngr_aws.backend import AwsProvider
from imbue.mngr_aws.backend import AwsProviderBackend
from imbue.mngr_aws.backend import ParsedAwsBuildOptions
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_aws.config import ExistingSecurityGroup
from imbue.mngr_aws.testing import clear_aws_env


def test_backend_build_args_help_mentions_aws_specific_args() -> None:
    """The build-args help is consumed by ``mngr help create`` and is the only
    user-facing surface that describes EC2-specific build-arg overrides. It
    must mention the AWS-specific flags (--aws-region, --aws-instance-type,
    --aws-ami) and the fact that the AMI override falls back to the provider
    config's default_ami_id when omitted.
    """
    help_text = AwsProviderBackend.get_build_args_help()
    assert "EC2-specific" in help_text, "help should call out that these args are EC2-specific"
    assert "--aws-region=REGION" in help_text
    assert "--aws-instance-type=TYPE" in help_text
    assert "--aws-ami=AMI-ID" in help_text
    assert "default_ami_id" in help_text


def _build_provider(mngr_ctx: MngrContext, *, auto_shutdown_seconds: int | None) -> AwsProvider:
    """Construct an AwsProvider with the given auto-shutdown setting.

    Uses a plain boto3 Session and a placeholder AMI: this helper is only
    used by tests that exercise the pytest-detection guard, which fires
    before any EC2 API call, so the session/AMI are never touched.
    """
    config = AwsProviderConfig(
        backend=AWS_BACKEND_NAME,
        default_ami_id="ami-placeholder",
        auto_shutdown_seconds=auto_shutdown_seconds,
    )
    client = AwsVpsClient(
        session=boto3.Session(region_name=config.default_region),
        region=config.default_region,
        ami_id="ami-placeholder",
        security_group=ExistingSecurityGroup(id="sg-placeholder"),
    )
    return AwsProvider(
        name=ProviderInstanceName("aws-test"),
        host_dir=config.host_dir,
        mngr_ctx=mngr_ctx,
        config=config,
        vps_client=client,
        aws_client=client,
        aws_config=config,
    )


def test_validate_provider_args_under_pytest_raises_when_unset(
    temp_mngr_ctx: MngrContext,
) -> None:
    """The pre-create hook fires when auto_shutdown_seconds is None (the config default).

    Regression: a release test that forgets to set auto_shutdown_seconds on
    the AWS provider config would silently launch instances with no self-
    termination safety net. The hook must abort the launch before any
    EC2 API call so the leak window is zero.
    """
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=None)
    with pytest.raises(MngrError, match="auto_shutdown_seconds"):
        provider._validate_provider_args_for_create()


def test_validate_provider_args_under_pytest_accepts_positive(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Properly configured tests pass the hook and proceed to instance creation."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    # No exception raised.
    provider._validate_provider_args_for_create()


def test_validate_provider_args_under_pytest_raises_when_zero(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Zero (and negatives) are explicitly rejected, not silently treated as unset."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=0)
    with pytest.raises(MngrError, match="auto_shutdown_seconds"):
        provider._validate_provider_args_for_create()


# =============================================================================
# AWS build-args parser (--aws-region, --aws-instance-type, --aws-ami, --git-depth)
# =============================================================================


def test_parse_build_args_uses_defaults_when_none(temp_mngr_ctx: MngrContext) -> None:
    """No build args -> region / instance-type come from the provider config; ami override stays None."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    parsed = provider._parse_build_args(None)
    assert parsed.region == provider.aws_config.default_region
    assert parsed.plan == provider.aws_config.default_instance_type
    assert parsed.ami_id_override is None
    assert parsed.git_depth is None
    assert parsed.docker_build_args == ()


def test_parse_build_args_accepts_aws_ami_override(temp_mngr_ctx: MngrContext) -> None:
    """`--aws-ami=ami-XYZ` lands on ami_id_override; other fields keep their defaults."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    parsed = provider._parse_build_args(["--aws-ami=ami-0123abcd"])
    assert parsed.ami_id_override == "ami-0123abcd"
    assert parsed.region == provider.aws_config.default_region
    assert parsed.plan == provider.aws_config.default_instance_type


def test_parse_build_args_extracts_all_aws_knobs_plus_docker_passthrough(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Each AWS-prefixed knob is peeled off; the remainder forwards to docker verbatim."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    parsed = provider._parse_build_args(
        [
            "--aws-region=us-west-2",
            "--aws-instance-type=t3.medium",
            "--aws-ami=ami-deadbeef",
            "--aws-spot",
            "--git-depth=1",
            "--file=Dockerfile",
            ".",
        ]
    )
    assert parsed.region == "us-west-2"
    assert parsed.plan == "t3.medium"
    assert parsed.ami_id_override == "ami-deadbeef"
    assert parsed.spot is True
    assert parsed.git_depth == 1
    assert parsed.docker_build_args == ("--file=Dockerfile", ".")


def test_parse_build_args_spot_defaults_false(temp_mngr_ctx: MngrContext) -> None:
    """Without --aws-spot, the parsed object reports spot=False (default on-demand)."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    parsed = provider._parse_build_args(None)
    assert parsed.spot is False


def test_parse_build_args_rejects_aws_spot_with_value(temp_mngr_ctx: MngrContext) -> None:
    """``--aws-spot`` is presence-only; passing a value (e.g. ``--aws-spot=true``) raises."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    with pytest.raises(MngrError, match="presence-only flag"):
        provider._parse_build_args(["--aws-spot=true"])


def test_parse_build_args_rejects_unknown_aws_flag(temp_mngr_ctx: MngrContext) -> None:
    """A typo / unknown --aws-* flag raises with the valid-args list, not silently forwarded."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    with pytest.raises(MngrError, match="Unknown aws build arg.*--aws-bogus"):
        provider._parse_build_args(["--aws-bogus=foo"])


def test_parse_build_args_rejects_dropped_vps_prefix(temp_mngr_ctx: MngrContext) -> None:
    """A caller still using --vps-region= gets the migration error pointing at the new name."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    with pytest.raises(MngrError, match="no longer supported"):
        provider._parse_build_args(["--vps-region=us-east-1"])


# =============================================================================
# Read paths surface auth failures as ProviderUnavailableError (not ...Empty)
# =============================================================================
#
# Missing credentials means the backend's state is *unknown* -- we couldn't
# authenticate, so any running instances are hidden from us. Per the
# ``ProviderEmptyError`` vs ``ProviderUnavailableError`` contract in
# ``mngr.errors``, that's the ``Unavailable`` shape: "could not be reached",
# agents may still exist. The shared discovery loop in
# ``mngr.api.list._construct_and_discover_for_provider`` catches
# ``ProviderUnavailableError`` via its generic catch-all and logs it at error
# level, so the misconfiguration is visible without the backend needing its
# own warning.
#
# AMI selection is a create-only concern (read paths do not need it to
# enumerate or reach existing instances). ``build_provider_instance`` never
# touches AMI resolution; that lives in ``AwsProvider._create_vps_instance``,
# the only call site, and a missing-AMI failure there raises ``MngrError``
# (a config error to be fixed). Create-path missing-creds is surfaced
# identically to read paths because the create flow calls
# ``build_provider_instance`` first -- no ``bootstrap_for_host_creation``
# override is needed, matching the Azure pattern.


def test_build_provider_instance_raises_unavailable_when_credentials_missing(
    monkeypatch: pytest.MonkeyPatch,
    temp_mngr_ctx: MngrContext,
) -> None:
    clear_aws_env(monkeypatch)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")
    config = AwsProviderConfig(backend=AWS_BACKEND_NAME, default_ami_id="ami-deadbeef")
    name = ProviderInstanceName("aws-test")

    with pytest.raises(ProviderUnavailableError):
        AwsProviderBackend.build_provider_instance(name=name, config=config, mngr_ctx=temp_mngr_ctx)


def test_build_provider_instance_does_not_touch_ami_resolution(
    monkeypatch: pytest.MonkeyPatch,
    temp_mngr_ctx: MngrContext,
) -> None:
    """A provider with valid creds but no AMI configured must still list/discover.

    AMI is a create-only concern; resolving it during ``build_provider_instance``
    would misclassify a misconfigured-AMI provider as unreachable and hide its
    already-running instances from ``mngr list`` / ``connect`` / ``gc``. This
    test pins the contract: build must succeed when only credentials resolve.
    """
    clear_aws_env(monkeypatch)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    config = AwsProviderConfig(
        backend=AWS_BACKEND_NAME,
        default_ami_id="",
        default_ami_by_region={},
    )
    name = ProviderInstanceName("aws-test")

    provider = AwsProviderBackend.build_provider_instance(name=name, config=config, mngr_ctx=temp_mngr_ctx)

    assert isinstance(provider, AwsProvider)


def test_create_vps_instance_raises_mngr_error_when_no_ami_configured(
    monkeypatch: pytest.MonkeyPatch,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Missing AMI is a create-time config error (MngrError), not a state signal.

    Distinct from the missing-creds case: ``ProviderUnavailableError`` would
    misclassify "I have valid creds but the operator forgot to pin a
    ``default_ami_id``" as an unreachable backend. The right shape at the
    create path is a plain ``MngrError`` carrying the actionable how-to-fix
    from ``AwsProviderConfig.get_ami_id_for_region``. The create flow's
    ``create_host`` except handler cleans up any SSH key uploaded before this
    raise, so no leak.
    """
    clear_aws_env(monkeypatch)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    config = AwsProviderConfig(
        backend=AWS_BACKEND_NAME,
        default_ami_id="",
        default_ami_by_region={},
    )
    name = ProviderInstanceName("aws-test")
    provider = AwsProviderBackend.build_provider_instance(name=name, config=config, mngr_ctx=temp_mngr_ctx)
    assert isinstance(provider, AwsProvider)
    parsed = ParsedAwsBuildOptions(
        region=config.default_region, plan=config.default_instance_type, docker_build_args=()
    )

    with pytest.raises(MngrError, match="No AMI configured"):
        provider._create_vps_instance(parsed=parsed, label="test", user_data="", ssh_key_ids=(), tags={})
