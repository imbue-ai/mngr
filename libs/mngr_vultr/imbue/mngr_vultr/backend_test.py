"""Tests for Vultr provider backend registration."""

from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_vultr.backend import VULTR_BACKEND_NAME
from imbue.mngr_vultr.backend import VultrProviderBackend
from imbue.mngr_vultr.backend import register_provider_backend
from imbue.mngr_vultr.config import VultrProviderConfig


def test_backend_name() -> None:
    assert VultrProviderBackend.get_name() == ProviderBackendName("vultr")


def test_backend_name_constant() -> None:
    assert VULTR_BACKEND_NAME == ProviderBackendName("vultr")


def test_backend_description() -> None:
    desc = VultrProviderBackend.get_description()
    assert "Vultr" in desc
    assert "Docker" in desc


def test_backend_config_class() -> None:
    config_cls = VultrProviderBackend.get_config_class()
    assert config_cls is VultrProviderConfig


def test_backend_build_args_help() -> None:
    help_text = VultrProviderBackend.get_build_args_help()
    assert "--vps-region" in help_text
    assert "--vps-plan" in help_text
    assert "--vps-os" in help_text


def test_backend_start_args_help() -> None:
    help_text = VultrProviderBackend.get_start_args_help()
    assert "docker run" in help_text


def test_register_provider_backend_returns_tuple() -> None:
    result = register_provider_backend()
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert result[0] is VultrProviderBackend
    assert result[1] is VultrProviderConfig
