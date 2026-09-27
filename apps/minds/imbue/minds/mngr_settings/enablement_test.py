import tomllib
from pathlib import Path

import pytest

from imbue.minds.bootstrap import MindsRoot
from imbue.minds.mngr_settings.enablement import set_provider_is_enabled
from imbue.minds.mngr_settings.imbue_cloud_accounts import set_imbue_cloud_provider_for_account
from imbue.minds.testing import stub_mngr_host_dir

_FAKE_CONNECTOR_URL = "https://test--rsc-api.modal.run"


def test_set_provider_is_enabled_flips_is_enabled_on_existing_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    set_imbue_cloud_provider_for_account(
        "alice@example.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root=MindsRoot("minds-dev-tname"),
    )

    changed = set_provider_is_enabled("imbue_cloud_alice-example-com", False, root=MindsRoot("minds-dev-tname"))
    assert changed is True
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud_alice-example-com"]["is_enabled"] is False

    # Idempotent: setting to the same value is a no-op.
    assert set_provider_is_enabled("imbue_cloud_alice-example-com", False, root=MindsRoot("minds-dev-tname")) is False

    # Re-enabling flips the bit back.
    assert set_provider_is_enabled("imbue_cloud_alice-example-com", True, root=MindsRoot("minds-dev-tname")) is True
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud_alice-example-com"]["is_enabled"] is True


def test_set_provider_is_enabled_creates_override_block_for_missing_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When [providers.<name>] doesn't exist in minds' settings, it's created with just is_enabled."""
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")

    changed = set_provider_is_enabled("docker", False, root=MindsRoot("minds-dev-tname"))
    assert changed is True
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["docker"] == {"is_enabled": False}

    # Now re-enable: same block is updated.
    changed = set_provider_is_enabled("docker", True, root=MindsRoot("minds-dev-tname"))
    assert changed is True
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["docker"] == {"is_enabled": True}


def test_set_provider_is_enabled_creates_settings_file_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If minds' active settings file does not yet exist, it is created."""
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    # Make sure no file exists yet
    if settings_path.exists():
        settings_path.unlink()

    changed = set_provider_is_enabled("modal", False, root=MindsRoot("minds-dev-tname"))
    assert changed is True
    assert settings_path.exists()
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["modal"] == {"is_enabled": False}
