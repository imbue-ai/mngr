"""Tests for AWS provider backend registration."""

import boto3
import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_aws.backend import AWS_BACKEND_NAME
from imbue.mngr_aws.backend import AwsProvider
from imbue.mngr_aws.backend import AwsProviderBackend
from imbue.mngr_aws.backend import register_provider_backend
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_aws.config import ExistingSecurityGroup


def test_backend_name() -> None:
    assert AwsProviderBackend.get_name() == ProviderBackendName("aws")


def test_backend_name_constant() -> None:
    assert AWS_BACKEND_NAME == ProviderBackendName("aws")


def test_backend_description() -> None:
    desc = AwsProviderBackend.get_description()
    assert "AWS" in desc
    assert "Docker" in desc


def test_backend_config_class() -> None:
    config_cls = AwsProviderBackend.get_config_class()
    assert config_cls is AwsProviderConfig


def test_backend_build_args_help() -> None:
    help_text = AwsProviderBackend.get_build_args_help()
    assert "--vps-region" in help_text
    assert "--vps-plan" in help_text
    assert "us-east-1" in help_text
    assert "t3.small" in help_text


def test_backend_start_args_help() -> None:
    help_text = AwsProviderBackend.get_start_args_help()
    assert "docker run" in help_text


def test_register_provider_backend_returns_tuple() -> None:
    result = register_provider_backend()
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert result[0] is AwsProviderBackend
    assert result[1] is AwsProviderConfig


def _build_provider(mngr_ctx: MngrContext, *, auto_shutdown_minutes: int | None) -> AwsProvider:
    """Construct an AwsProvider with the given auto-shutdown setting.

    Uses a plain boto3 Session and a placeholder AMI: this helper is only
    used by tests that exercise the pytest-detection guard, which fires
    before any EC2 API call, so the session/AMI are never touched.
    """
    config = AwsProviderConfig(
        backend=AWS_BACKEND_NAME,
        default_ami_id="ami-placeholder",
        auto_shutdown_minutes=auto_shutdown_minutes,
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


def test_get_effective_auto_shutdown_minutes_under_pytest_raises_when_unset(
    temp_mngr_ctx: MngrContext,
) -> None:
    """The guard fires when auto_shutdown_minutes is None (the config default).

    Regression: a release test that forgets to set auto_shutdown_minutes on
    the AWS provider config would silently launch instances with no self-
    termination safety net. The guard must abort the launch before any
    EC2 API call so the leak window is zero.
    """
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=None)
    with pytest.raises(MngrError, match="auto_shutdown_minutes"):
        provider._get_effective_auto_shutdown_minutes()


def test_get_effective_auto_shutdown_minutes_under_pytest_accepts_positive(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Properly configured tests pass the guard and propagate the value to cloud-init."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
    assert provider._get_effective_auto_shutdown_minutes() == 60


def test_get_effective_auto_shutdown_minutes_under_pytest_raises_when_zero(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Zero (and negatives) are explicitly rejected, not silently treated as unset."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=0)
    with pytest.raises(MngrError, match="auto_shutdown_minutes"):
        provider._get_effective_auto_shutdown_minutes()
