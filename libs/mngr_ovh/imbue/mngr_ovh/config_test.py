"""Tests for OVH provider configuration."""

import pytest
from pydantic import SecretStr

from imbue.mngr_ovh.config import OvhPricingMode
from imbue.mngr_ovh.config import OvhProviderConfig


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip any OVH_* env vars so explicit-config tests aren't polluted."""
    for name in (
        "OVH_ENDPOINT",
        "OVH_APPLICATION_KEY",
        "OVH_APPLICATION_SECRET",
        "OVH_APP_KEY",
        "OVH_APP_SECRET",
        "OVH_CONSUMER_KEY",
        "OVH_CLIENT_ID",
        "OVH_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_config_values(clean_env: None) -> None:
    config = OvhProviderConfig()
    assert config.endpoint == "ovh-us"
    assert config.default_region == "US-EAST-VA"
    assert config.default_plan == "vps-2025-model1"
    assert config.default_image_name == "Debian 12 - Docker"
    # Matches OVH's `Debian 12 - Docker` image which installs the rebuild
    # SSH key only under /home/debian/.ssh; the provider sudo-copies that
    # key to /root during provisioning so the rest of the flow works as root.
    assert config.bootstrap_ssh_user == "debian"
    assert config.pricing_mode == OvhPricingMode.DEFAULT
    assert config.pricing_mode.to_wire_value() == "default"
    assert config.duration == "P1M"
    assert config.instance_boot_timeout == 600.0
    assert config.ovh_subsidiary == "US"
    assert config.application_key is None
    # Pool-workload-tuned: tight enough that destroy + create in the same
    # day reuses the cancelled VPS even when it has only a few hours of
    # paid month left.
    assert config.recycle_safety_margin_hours == 2


def test_backend_name_defaults_to_ovh(clean_env: None) -> None:
    config = OvhProviderConfig()
    assert str(config.backend) == "ovh"


def test_resolve_endpoint_uses_env_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVH_ENDPOINT", "ovh-eu")
    config = OvhProviderConfig()
    assert config.resolve_endpoint() == "ovh-eu"


def test_resolve_endpoint_falls_back_to_config(clean_env: None) -> None:
    config = OvhProviderConfig(endpoint="ovh-ca")
    assert config.resolve_endpoint() == "ovh-ca"


def test_explicit_credentials_take_precedence_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVH_APPLICATION_KEY", "env-ak")
    config = OvhProviderConfig(application_key=SecretStr("config-ak"))
    kwargs = config.resolve_python_ovh_kwargs()
    assert kwargs["application_key"] == "config-ak"


def test_env_aliases_for_application_key(monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
    monkeypatch.setenv("OVH_APP_KEY", "alias-ak")
    monkeypatch.setenv("OVH_APP_SECRET", "alias-as")
    config = OvhProviderConfig()
    kwargs = config.resolve_python_ovh_kwargs()
    assert kwargs["application_key"] == "alias-ak"
    assert kwargs["application_secret"] == "alias-as"


def test_canonical_env_takes_precedence_over_alias(monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
    monkeypatch.setenv("OVH_APPLICATION_KEY", "canonical-ak")
    monkeypatch.setenv("OVH_APP_KEY", "alias-ak")
    config = OvhProviderConfig()
    kwargs = config.resolve_python_ovh_kwargs()
    assert kwargs["application_key"] == "canonical-ak"


def test_resolve_kwargs_collects_oauth2(monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
    monkeypatch.setenv("OVH_CLIENT_ID", "cid")
    monkeypatch.setenv("OVH_CLIENT_SECRET", "csec")
    config = OvhProviderConfig()
    kwargs = config.resolve_python_ovh_kwargs()
    assert kwargs["client_id"] == "cid"
    assert kwargs["client_secret"] == "csec"
    assert "application_key" not in kwargs


def test_resolve_kwargs_empty_when_no_credentials(clean_env: None) -> None:
    """No env, no explicit config: only ``endpoint`` is returned (python-ovh will then try ~/.ovh.conf)."""
    config = OvhProviderConfig()
    kwargs = config.resolve_python_ovh_kwargs()
    assert kwargs == {"endpoint": "ovh-us"}


def test_has_explicit_credentials_false_when_nothing_set(clean_env: None) -> None:
    assert OvhProviderConfig().has_explicit_credentials() is False


def test_has_explicit_credentials_true_when_ak_set(clean_env: None) -> None:
    config = OvhProviderConfig(application_key=SecretStr("ak"))
    assert config.has_explicit_credentials() is True


def test_has_explicit_credentials_true_when_oauth_env(monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
    monkeypatch.setenv("OVH_CLIENT_ID", "cid")
    assert OvhProviderConfig().has_explicit_credentials() is True
