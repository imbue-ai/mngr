import pytest
from azure.identity import DefaultAzureCredential

from imbue.mngr_azure.config import AzureProviderConfig


def test_default_config_values() -> None:
    config = AzureProviderConfig(subscription_id="sub-123")
    assert config.default_region == "westus"
    assert config.default_vm_size == "Standard_B2s"
    assert config.resource_group == "mngr"
    assert config.vnet_name == "mngr-vnet"
    assert config.subnet_name == "mngr-subnet"
    assert config.nsg_name == "mngr-nsg"
    assert config.os_disk_type == "StandardSSD_LRS"
    # Fail-closed: no SSH CIDRs by default.
    assert config.allowed_ssh_cidrs == ()


def test_backend_name_defaults_to_azure() -> None:
    config = AzureProviderConfig(subscription_id="sub-123")
    assert str(config.backend) == "azure"


def test_default_image_is_ubuntu_lts() -> None:
    config = AzureProviderConfig()
    assert config.image_publisher == "Canonical"
    assert config.image_offer == "ubuntu-24_04-lts"
    assert config.image_sku == "server"
    assert config.image_version == "latest"


def test_get_subscription_id_prefers_config() -> None:
    config = AzureProviderConfig(subscription_id="sub-from-config")
    assert config.get_subscription_id() == "sub-from-config"


def test_get_subscription_id_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-from-env")
    config = AzureProviderConfig()
    assert config.get_subscription_id() == "sub-from-env"


def test_get_subscription_id_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    config = AzureProviderConfig()
    with pytest.raises(ValueError, match="No Azure subscription_id configured"):
        config.get_subscription_id()


def test_get_credential_returns_default_azure_credential() -> None:
    config = AzureProviderConfig(subscription_id="sub-123")
    assert isinstance(config.get_credential(), DefaultAzureCredential)
