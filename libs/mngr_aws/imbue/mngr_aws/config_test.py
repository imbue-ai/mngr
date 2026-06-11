"""Tests for AWS provider configuration."""

from pathlib import Path

import pytest

from imbue.mngr.config.data_types import ScalarTuple
from imbue.mngr.config.data_types import detect_settings_narrowing
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_aws.config import AutoCreateSecurityGroup
from imbue.mngr_aws.config import AwsProviderConfig
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
    assert config.auto_shutdown_minutes is None


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


def test_get_ami_id_for_region_uses_default_ami_id() -> None:
    config = AwsProviderConfig(default_ami_id="ami-deadbeef")
    assert config.get_ami_id_for_region("us-east-1") == "ami-deadbeef"
    assert config.get_ami_id_for_region("eu-west-1") == "ami-deadbeef"


def test_get_ami_id_for_region_uses_region_map() -> None:
    config = AwsProviderConfig(default_ami_by_region={"us-east-1": "ami-east", "eu-west-1": "ami-eu"})
    assert config.get_ami_id_for_region("us-east-1") == "ami-east"
    assert config.get_ami_id_for_region("eu-west-1") == "ami-eu"


def test_get_ami_id_for_region_raises_when_missing() -> None:
    config = AwsProviderConfig(default_ami_by_region={})
    with pytest.raises(ValueError, match="No AMI configured"):
        config.get_ami_id_for_region("us-east-1")


def test_get_ami_id_explicit_takes_precedence_over_region_map() -> None:
    config = AwsProviderConfig(
        default_ami_id="ami-override",
        default_ami_by_region={"us-east-1": "ami-region-specific"},
    )
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
    ``detect_settings_narrowing`` treats it as scalar replacement. A plain-tuple
    override of the same shape (no marker -- e.g. via ``model_construct``) still
    narrows, proving the marker is the discriminator and not some incidental
    property.
    """
    project = AwsProviderConfig.model_validate({"backend": "aws"})
    local_override = AwsProviderConfig.model_validate({"backend": "aws", "allowed_ssh_cidrs": ["203.0.113.4/32"]})
    assert detect_settings_narrowing(project, local_override) == []
    unmarked_override = AwsProviderConfig.model_construct(
        backend=ProviderBackendName("aws"), allowed_ssh_cidrs=("203.0.113.4/32",)
    )
    assert detect_settings_narrowing(project, unmarked_override) == ["allowed_ssh_cidrs"]
