import json
import threading
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
    # Open by default (fail-open) to match the AWS / GCP providers; a warning is
    # logged at prepare/create time and production users are expected to tighten it.
    assert config.allowed_ssh_cidrs == ("0.0.0.0/0",)


def test_backend_name_defaults_to_azure() -> None:
    config = AzureProviderConfig(subscription_id="sub-123")
    assert str(config.backend) == "azure"


def test_default_image_is_debian_12() -> None:
    config = AzureProviderConfig()
    assert config.image_publisher == "Debian"
    assert config.image_offer == "debian-12"
    assert config.image_sku == "12-gen2"
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
    # A *persistently* corrupt / non-UTF-8 azureProfile.json must resolve to None
    # (the file is "unreadable") rather than raising UnicodeDecodeError on the
    # mngr hot path. The bounded retry exhausts and still returns None.
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "azureProfile.json").write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    assert read_az_cli_default_subscription() is None


def test_read_az_cli_default_subscription_retries_torn_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A read that races the az CLI's in-place rewrite can momentarily see a
    # truncated file. Such a torn read must be retried -- not taken as "no
    # subscription" -- so the resolved id still comes back once the write lands.
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    profile_path = tmp_path / "azureProfile.json"
    # Start mid-write: a valid prefix that is truncated, so json.loads fails.
    profile_path.write_text('{"subscriptions": [{"id": "sub-rec', encoding="utf-8-sig")
    valid = json.dumps({"subscriptions": [{"id": "sub-recovered", "isDefault": True, "state": "Enabled"}]})

    # Complete the file from another thread. The reader's first attempt runs
    # synchronously here while the freshly-spawned thread is still starting up,
    # so it reliably hits the torn file; the completed write then lands well
    # within the reader's retry budget (~0.1s), so a later attempt succeeds. No
    # sleep is needed on either side for this ordering, and correct code always
    # ends up reading the completed file, so the test does not flake.
    writer = threading.Thread(target=lambda: profile_path.write_text(valid, encoding="utf-8-sig"))
    writer.start()
    try:
        assert read_az_cli_default_subscription() == "sub-recovered"
    finally:
        writer.join()


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


def test_resolve_state_storage_account_name_derives_valid_name() -> None:
    config = AzureProviderConfig(subscription_id="sub-123", resource_group="mngr")
    name = config.resolve_state_storage_account_name("sub-123")
    assert name.startswith("mngrst")
    assert 3 <= len(name) <= 24
    assert name.isalnum() and name.islower()


def test_resolve_state_storage_account_name_is_deterministic_per_scope() -> None:
    config = AzureProviderConfig(subscription_id="sub-123", resource_group="mngr")
    first = config.resolve_state_storage_account_name("sub-123")
    second = config.resolve_state_storage_account_name("sub-123")
    assert first == second
    # A different subscription or resource group yields a different account name.
    other_sub = config.resolve_state_storage_account_name("sub-999")
    other_rg = AzureProviderConfig(resource_group="other").resolve_state_storage_account_name("sub-123")
    assert first != other_sub
    assert first != other_rg


def test_resolve_state_storage_account_name_honors_explicit_override() -> None:
    config = AzureProviderConfig(subscription_id="sub-123", state_storage_account_name="mngrstmyteam")
    assert config.resolve_state_storage_account_name("sub-123") == "mngrstmyteam"


def test_build_state_bucket_uses_resolved_name_and_scope() -> None:
    config = AzureProviderConfig(subscription_id="sub-123", resource_group="mngr", default_region="westus")
    bucket = config.build_state_bucket("sub-123")
    assert bucket.account_name == config.resolve_state_storage_account_name("sub-123")
    assert bucket.subscription_id == "sub-123"
    assert bucket.resource_group == "mngr"
    assert bucket.region == "westus"
