"""Tests for Vultr provider configuration."""

import pytest
from pydantic import SecretStr

from imbue.mngr_vultr.config import VultrProviderConfig


def test_default_config_values() -> None:
    config = VultrProviderConfig()
    assert config.default_region == "ewr"
    assert config.default_plan == "vc2-1c-1gb"
    assert config.default_os_id == 2136
    assert config.api_key is None


def test_get_api_key_from_config() -> None:
    config = VultrProviderConfig(api_key=SecretStr("test-api-key-123"))
    assert config.get_api_key() == "test-api-key-123"


def test_get_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VULTR_API_KEY", "env-api-key-456")
    config = VultrProviderConfig()
    assert config.get_api_key() == "env-api-key-456"


def test_get_api_key_config_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VULTR_API_KEY", "env-key")
    config = VultrProviderConfig(api_key=SecretStr("config-key"))
    assert config.get_api_key() == "config-key"


def test_get_api_key_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VULTR_API_KEY", raising=False)
    config = VultrProviderConfig()
    with pytest.raises(ValueError, match="Vultr API key not configured"):
        config.get_api_key()


def test_backend_name_defaults_to_vultr() -> None:
    config = VultrProviderConfig()
    assert str(config.backend) == "vultr"
