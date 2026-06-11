import json
from pathlib import Path

import pytest
from azure.identity import DefaultAzureCredential

from imbue.mngr_azure.config import AzureProviderConfig
from imbue.mngr_azure.config import read_az_cli_default_subscription


def _write_az_profile(config_dir: Path, subscriptions: list[dict[str, object]]) -> None:
    # az writes azureProfile.json with a UTF-8 BOM, which read_az_cli_default_subscription decodes.
    (config_dir / "azureProfile.json").write_text(json.dumps({"subscriptions": subscriptions}), encoding="utf-8-sig")


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


def test_get_subscription_id_falls_back_to_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-from-env")
    # Point AZURE_CONFIG_DIR at an empty dir so the env var (not a real az profile) is what resolves.
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    config = AzureProviderConfig()
    assert config.get_subscription_id() == "sub-from-env"


def test_get_subscription_id_falls_back_to_az_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    _write_az_profile(
        tmp_path,
        [
            {"id": "other-sub", "isDefault": False, "state": "Enabled"},
            {"id": "active-sub", "isDefault": True, "state": "Enabled"},
        ],
    )
    config = AzureProviderConfig()
    assert config.get_subscription_id() == "active-sub"


def test_config_subscription_id_beats_az_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    _write_az_profile(tmp_path, [{"id": "az-default", "isDefault": True, "state": "Enabled"}])
    config = AzureProviderConfig(subscription_id="explicit")
    assert config.get_subscription_id() == "explicit"


def test_read_az_cli_default_subscription_ignores_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    _write_az_profile(tmp_path, [{"id": "disabled-default", "isDefault": True, "state": "Disabled"}])
    assert read_az_cli_default_subscription() is None


def test_read_az_cli_default_subscription_none_when_no_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    assert read_az_cli_default_subscription() is None


def test_read_az_cli_default_subscription_none_on_undecodable_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A corrupt / non-UTF-8 azureProfile.json must resolve to None (the file is
    # "unreadable") rather than raising UnicodeDecodeError on the mngr hot path.
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "azureProfile.json").write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    assert read_az_cli_default_subscription() is None


def test_get_subscription_id_raises_when_unresolvable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    # Empty AZURE_CONFIG_DIR -> no az profile -> nothing resolves.
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    config = AzureProviderConfig()
    with pytest.raises(ValueError, match="No Azure subscription resolved"):
        config.get_subscription_id()


def test_get_credential_returns_default_azure_credential() -> None:
    config = AzureProviderConfig(subscription_id="sub-123")
    assert isinstance(config.get_credential(), DefaultAzureCredential)
