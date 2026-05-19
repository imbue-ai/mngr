"""Tests for OVH provider backend registration."""

from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_ovh.backend import OVH_BACKEND_NAME
from imbue.mngr_ovh.backend import OvhProviderBackend
from imbue.mngr_ovh.backend import register_provider_backend
from imbue.mngr_ovh.config import OvhProviderConfig


def test_backend_name() -> None:
    assert OvhProviderBackend.get_name() == ProviderBackendName("ovh")


def test_backend_name_constant() -> None:
    assert OVH_BACKEND_NAME == ProviderBackendName("ovh")


def test_backend_description() -> None:
    desc = OvhProviderBackend.get_description()
    assert "OVH" in desc
    assert "Docker" in desc


def test_backend_config_class() -> None:
    assert OvhProviderBackend.get_config_class() is OvhProviderConfig


def test_backend_build_args_help() -> None:
    help_text = OvhProviderBackend.get_build_args_help()
    assert "--vps-datacenter" in help_text
    assert "--vps-plan" in help_text
    assert "--vps-os" in help_text


def test_backend_start_args_help() -> None:
    assert "docker run" in OvhProviderBackend.get_start_args_help()


def test_register_provider_backend_returns_tuple() -> None:
    result = register_provider_backend()
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert result[0] is OvhProviderBackend
    assert result[1] is OvhProviderConfig
