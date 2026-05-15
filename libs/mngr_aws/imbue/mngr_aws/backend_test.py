"""Tests for AWS provider backend registration."""

from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_aws.backend import AWS_BACKEND_NAME
from imbue.mngr_aws.backend import AwsProviderBackend
from imbue.mngr_aws.backend import register_provider_backend
from imbue.mngr_aws.config import AwsProviderConfig


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
