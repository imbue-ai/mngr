"""Tests for AWS provider configuration."""

from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from imbue.mngr.config.data_types import ScalarTuple
from imbue.mngr.config.overlay_merge import merge_models_via_overlay
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_aws.config import AutoCreateSecurityGroup
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_aws.config import DEFAULT_AMI_BY_REGION
from imbue.mngr_aws.testing import clear_aws_env


def write_default_credentials_file(directory: Path) -> Path:
    """Write a minimal AWS shared-credentials file with a ``[default]`` profile.

    The returned path is suitable for use with ``AWS_SHARED_CREDENTIALS_FILE``.
    """
    creds_path = directory / "credentials"
    creds_path.write_text(
        "[default]\naws_access_key_id = AKIATEST\naws_secret_access_key = test-secret\n",
        encoding="utf-8",
    )
    return creds_path


def test_default_config_values() -> None:
    config = AwsProviderConfig()
    assert config.default_region == "us-east-1"
    assert config.default_instance_type == "t3.small"
    # Default security_group is AutoCreate with name 'mngr-aws'.
    assert isinstance(config.security_group, AutoCreateSecurityGroup)
    assert config.security_group.name == "mngr-aws"
    # Default is 0.0.0.0/0 to match Vultr/OVH reachability norms in this monorepo
    # (those providers ship no managed firewall). Production users should tighten.
    assert config.allowed_ssh_cidrs == ("0.0.0.0/0",)
    assert config.associate_public_ip is True
    assert config.root_volume_size_gb == 30
    assert config.root_volume_type == "gp3"
    assert config.auto_shutdown_seconds is None


def test_backend_name_defaults_to_aws() -> None:
    config = AwsProviderConfig()
    assert str(config.backend) == "aws"


def test_get_session_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_aws_env(monkeypatch)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")
    config = AwsProviderConfig()
    with pytest.raises(ValueError, match="AWS credentials not configured"):
        config.get_session()


def test_get_session_returns_session_with_region(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_aws_env(monkeypatch)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    config = AwsProviderConfig(default_region="us-west-2")
    session = config.get_session()
    assert session.region_name == "us-west-2"
    creds = session.get_credentials()
    assert creds is not None


def test_resolve_state_bucket_name_uses_explicit_override() -> None:
    """An explicit ``state_bucket_name`` wins and needs no STS call."""
    config = AwsProviderConfig(state_bucket_name="my-custom-bucket")
    session = boto3.Session(region_name="us-east-1")
    assert config.resolve_state_bucket_name(session) == "my-custom-bucket"


def test_resolve_state_bucket_name_derives_from_account_and_region() -> None:
    """When unset, the bucket name derives as mngr-state-<account_id>-<region>."""
    with mock_aws():
        session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-west-2")
        # moto's default account id is 123456789012.
        config = AwsProviderConfig(default_region="us-west-2")
        assert config.resolve_state_bucket_name(session) == "mngr-state-123456789012-us-west-2"


def test_resolve_state_bucket_name_region_override_matches_bucket_location() -> None:
    """A region override embeds that region in the derived name (so the operator CLI's
    name and the bucket's actual region agree when ``--region`` differs from default)."""
    with mock_aws():
        session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1")
        config = AwsProviderConfig(default_region="us-east-1")
        assert config.resolve_state_bucket_name(session, "us-west-2") == "mngr-state-123456789012-us-west-2"
        # No override falls back to default_region (the runtime path).
        assert config.resolve_state_bucket_name(session) == "mngr-state-123456789012-us-east-1"


def test_build_state_bucket_returns_bucket_when_resolvable() -> None:
    """``build_state_bucket`` returns an S3StateBucket bound to the derived name/region."""
    with mock_aws():
        session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1")
        config = AwsProviderConfig(state_bucket_name="my-bucket", default_region="us-east-1")
        bucket = config.build_state_bucket(session)
        assert bucket is not None
        assert bucket.bucket_name == "my-bucket"
        assert bucket.region == "us-east-1"


def test_get_ami_id_for_region_uses_default_ami_id() -> None:
    config = AwsProviderConfig(default_ami_id="ami-deadbeef")
    assert config.get_ami_id_for_region("us-east-1") == "ami-deadbeef"
    assert config.get_ami_id_for_region("eu-west-1") == "ami-deadbeef"


def test_get_ami_id_for_region_uses_pinned_region_default() -> None:
    config = AwsProviderConfig()
    for region, ami_id in DEFAULT_AMI_BY_REGION.items():
        assert config.get_ami_id_for_region(region) == ami_id


def test_get_ami_id_for_region_raises_when_missing() -> None:
    config = AwsProviderConfig()
    with pytest.raises(ValueError, match="No AMI configured"):
        config.get_ami_id_for_region("ap-south-1")


def test_get_ami_id_explicit_takes_precedence_over_region_default() -> None:
    config = AwsProviderConfig(default_ami_id="ami-override")
    assert config.get_ami_id_for_region("us-east-1") == "ami-override"


def test_allowed_ssh_cidrs_parses_to_scalar_tuple() -> None:
    """``allowed_ssh_cidrs`` is declared ``ScalarStrTuple``, so ``model_validate``
    (the path ``_parse_providers`` uses) marks both the explicit value and the
    default as a ``ScalarTuple``.

    The marker is what lets the settings-narrowing guard treat the field as
    replace-by-default (see ``test_local_layer_tightening_allowed_ssh_cidrs_does_not_narrow``).
    """
    explicit = AwsProviderConfig.model_validate({"allowed_ssh_cidrs": ["203.0.113.4/32"]})
    assert explicit.allowed_ssh_cidrs == ("203.0.113.4/32",)
    assert isinstance(explicit.allowed_ssh_cidrs, ScalarTuple)
    defaulted = AwsProviderConfig.model_validate({})
    assert isinstance(defaulted.allowed_ssh_cidrs, ScalarTuple)


def test_local_layer_tightening_allowed_ssh_cidrs_does_not_narrow() -> None:
    """A developer's settings.local.toml narrowing ``allowed_ssh_cidrs`` to their
    own IP must replace the committed default, not trip the settings-narrowing
    guard.

    The committed ``[providers.aws]`` block carries the non-empty default
    ``("0.0.0.0/0",)``; a local layer overrides it with a single IP. Because the
    field is a ``ScalarStrTuple``, the validated override is a ``ScalarTuple`` and
    the overlay narrowing guard treats it as scalar replacement (the pipeline
    re-marks the ``Static*`` value stripped by ``model_dump``). A plain-tuple
    override of the same shape (no marker -- e.g. via ``model_construct``) still
    narrows, proving the marker is the discriminator and not some incidental
    property.
    """
    project = AwsProviderConfig.model_validate({"backend": "aws"})
    local_override = AwsProviderConfig.model_validate({"backend": "aws", "allowed_ssh_cidrs": ["203.0.113.4/32"]})
    assert _provider_narrowing_paths(project, local_override) == []
    unmarked_override = AwsProviderConfig.model_construct(
        backend=ProviderBackendName("aws"), allowed_ssh_cidrs=("203.0.113.4/32",)
    )
    assert _provider_narrowing_paths(project, unmarked_override) == ["allowed_ssh_cidrs"]


def _provider_narrowing_paths(base: AwsProviderConfig, override: AwsProviderConfig) -> list[str]:
    """The narrowing paths the production overlay merge surfaces for one provider config
    over another -- the same path the loader's cross-scope guard uses, exercised at the
    provider-config level."""
    _, narrowings = merge_models_via_overlay(base, override)
    return narrowings
