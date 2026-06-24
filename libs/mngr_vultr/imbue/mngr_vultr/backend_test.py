"""Tests for Vultr provider backend registration."""

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import ProviderNotAuthorizedError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
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
    assert "--vultr-region" in help_text
    assert "--vultr-plan" in help_text


def test_backend_start_args_help() -> None:
    help_text = VultrProviderBackend.get_start_args_help()
    assert "docker run" in help_text


def test_register_provider_backend_returns_tuple() -> None:
    result = register_provider_backend()
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert result[0] is VultrProviderBackend
    assert result[1] is VultrProviderConfig


def test_build_provider_instance_raises_not_authorized_when_api_key_missing(
    temp_mngr_ctx: MngrContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enabled-but-unconfigured Vultr provider surfaces as ProviderNotAuthorizedError, not a silent empty listing."""
    monkeypatch.delenv("VULTR_API_KEY", raising=False)
    name = ProviderInstanceName("vultr-no-key")
    config = VultrProviderConfig(backend=VULTR_BACKEND_NAME, api_key=None)

    with pytest.raises(ProviderNotAuthorizedError) as exc_info:
        VultrProviderBackend.build_provider_instance(name, config, temp_mngr_ctx)

    # A ProviderUnavailableError subclass so read paths keep the provider visible
    # (reported as unavailable) rather than dropping it from the listing.
    assert isinstance(exc_info.value, ProviderUnavailableError)
    assert exc_info.value.provider_name == name
