"""Tests for AWS provider configuration."""

from pathlib import Path

import pytest
from pydantic import SecretStr

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


def test_default_config_values() -> None:
    config = AwsProviderConfig()
    assert config.default_region == "us-east-1"
    assert config.default_plan == "t3.small"
    assert config.default_os_id == 0
    assert config.access_key_id is None
    assert config.secret_access_key is None
    assert config.security_group_name == "mngr-aws"
    assert config.allowed_ssh_cidr == "0.0.0.0/0"
    assert config.associate_public_ip is True
    assert config.root_volume_size_gb == 30
    assert config.root_volume_type == "gp3"


def test_backend_name_defaults_to_aws() -> None:
    config = AwsProviderConfig()
    assert str(config.backend) == "aws"


def test_has_resolvable_credentials_with_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    config = AwsProviderConfig(
        access_key_id=SecretStr("AKIATEST"),
        secret_access_key=SecretStr("secret"),
    )
    assert config.has_resolvable_credentials()


def test_has_resolvable_credentials_with_nonexistent_profile_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile name that boto3 cannot locate must not be reported as resolvable."""
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")
    config = AwsProviderConfig(profile="does-not-exist")
    assert not config.has_resolvable_credentials()


def test_has_resolvable_credentials_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    config = AwsProviderConfig()
    assert config.has_resolvable_credentials()


def test_has_resolvable_credentials_with_aws_profile_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """AWS_PROFILE pointing at the default profile resolves via boto3's chain."""
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("AWS_PROFILE", "default")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    # Point at a fake credentials file with a [default] profile so boto3 resolves the chain
    # without depending on the real ~/.aws/credentials on the test machine.
    creds_file = write_default_credentials_file(tmp_path)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds_file))
    config = AwsProviderConfig()
    assert config.has_resolvable_credentials()


def test_has_resolvable_credentials_returns_false_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env / file / explicit credentials, the check must return False."""
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")
    config = AwsProviderConfig()
    assert not config.has_resolvable_credentials()


def test_has_resolvable_credentials_finds_shared_credentials_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: previously the check missed credentials present only in ~/.aws/credentials."""
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    creds_file = write_default_credentials_file(tmp_path)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds_file))
    config = AwsProviderConfig()
    assert config.has_resolvable_credentials()


def test_get_session_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    # Block IMDS / shared-config-file lookups so boto3 truly has nothing.
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")
    config = AwsProviderConfig()
    with pytest.raises(ValueError, match="AWS credentials not configured"):
        config.get_session()


def test_get_session_with_explicit_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    config = AwsProviderConfig(
        access_key_id=SecretStr("AKIATEST"),
        secret_access_key=SecretStr("secret"),
        default_region="us-west-2",
    )
    session = config.get_session()
    assert session.region_name == "us-west-2"
    creds = session.get_credentials()
    assert creds is not None
    frozen = creds.get_frozen_credentials()
    assert frozen.access_key == "AKIATEST"
    assert frozen.secret_key == "secret"


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
