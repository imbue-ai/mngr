"""Tests for AWS provider configuration."""

import os
from pathlib import Path

import pytest

from imbue.mngr_aws.config import AutoCreateSecurityGroup
from imbue.mngr_aws.config import AwsProviderConfig


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


def _clear_aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every AWS_* env var so credential-chain probes start clean.

    boto3's chain inspects a dozen-plus env vars; clearing only those each
    test cares about by name leaks knowledge of the chain into tests. Wipe
    them all and let the test re-set only what it wants.
    """
    for key in list(os.environ.keys()):
        if key.startswith("AWS_"):
            monkeypatch.delenv(key, raising=False)


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
    # AWS raises the cloud-init slow-warning threshold above the VPS-Docker base
    # default (30s) because a cold EC2 instance legitimately takes 30-60s+.
    assert config.cloud_init_slow_warning_threshold_seconds == 90.0


def test_backend_name_defaults_to_aws() -> None:
    config = AwsProviderConfig()
    assert str(config.backend) == "aws"


def test_get_session_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_aws_env(monkeypatch)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")
    config = AwsProviderConfig()
    with pytest.raises(ValueError, match="AWS credentials not configured"):
        config.get_session()


def test_get_session_returns_session_with_region(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_aws_env(monkeypatch)
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
