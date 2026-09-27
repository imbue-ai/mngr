import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import MindsRoot
from imbue.minds.mngr_settings.enablement import set_provider_is_enabled
from imbue.minds.mngr_settings.errors import MindsSettingsError
from imbue.minds.mngr_settings.reconcile import ensure_mngr_settings
from imbue.minds.mngr_settings.reconcile import ensure_mngr_settings_before_mngr_import
from imbue.minds.testing import stub_mngr_host_dir


def _forget_imported_mngr_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop any already-imported mngr modules from sys.modules (restored after the test).

    The pytest process itself has legitimately imported mngr, but the entry point under test models a fresh ``minds`` CLI process where mngr must not be loaded yet.
    """
    for name in [name for name in sys.modules if name == "imbue.mngr" or name.startswith("imbue.mngr.")]:
        monkeypatch.delitem(sys.modules, name)


def test_ensure_before_mngr_import_raises_when_mngr_already_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "imbue.mngr.config", ModuleType("imbue.mngr.config"))
    with pytest.raises(MindsSettingsError, match="before importing mngr"):
        ensure_mngr_settings_before_mngr_import()


def test_ensure_before_mngr_import_noops_when_root_name_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _forget_imported_mngr_modules(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(MINDS_ROOT_NAME_ENV_VAR, raising=False)
    ensure_mngr_settings_before_mngr_import()
    assert not (tmp_path / ".minds").exists()


def test_ensure_before_mngr_import_reconciles_active_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _forget_imported_mngr_modules(monkeypatch)
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-dev-tname")
    ensure_mngr_settings_before_mngr_import()
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["plugins"]["recursive"]["enabled"] is False


def test_ensure_mngr_settings_writes_default_imbue_cloud_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``ensure_mngr_settings`` must suppress the default ``[providers.imbue_cloud]``
    instance so ``get_all_provider_instances`` doesn't auto-create one alongside
    the per-account ``imbue_cloud_<slug>`` entries.
    """
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    ensure_mngr_settings(MindsRoot("minds-dev-tname"))
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud"] == {"backend": "imbue_cloud", "is_enabled": False}
    assert parsed["plugins"]["recursive"]["enabled"] is False


def test_ensure_mngr_settings_keeps_default_aws_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The region-less default ``[providers.aws]`` stays suppressed.

    Otherwise ``get_all_provider_instances`` auto-creates it and its discovery
    fails every ``mngr list`` cycle ("credentials not configured"); the usable
    AWS providers are the bring-your-own-key ``byok-aws-<slug>`` account blocks.
    """
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    ensure_mngr_settings(MindsRoot("minds-dev-tname"))
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["aws"] == {"backend": "aws", "is_enabled": False}


def test_ensure_mngr_settings_removes_legacy_ambient_aws_region_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ambient ``aws-<region>`` blocks written by earlier builds are actively
    deleted at boot (the machine-credential AWS path was removed from minds;
    ``byok-aws-<slug>`` accounts are the only AWS path). BYOK blocks survive."""
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    settings_path.write_text(
        '[providers.aws-us-east-1]\nbackend = "aws"\ndefault_region = "us-east-1"\n\n'
        '[providers.byok-aws-mine]\nbackend = "aws"\ndefault_region = "us-east-1"\n'
    )
    ensure_mngr_settings(MindsRoot("minds-dev-tname"))
    parsed = tomllib.loads(settings_path.read_text())
    assert "aws-us-east-1" not in parsed["providers"]
    assert "byok-aws-mine" in parsed["providers"]


def test_ensure_mngr_settings_returns_whether_file_was_modified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first write returns True (callers should bounce ``mngr observe``);
    a repeat call on an already-shaped file returns False."""
    stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    assert ensure_mngr_settings(MindsRoot("minds-dev-tname")) is True
    assert ensure_mngr_settings(MindsRoot("minds-dev-tname")) is False


def test_ensure_mngr_settings_leaves_panel_disabled_modal_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A panel-toggled ``is_enabled = false`` on ``[providers.modal]`` is a valid desired
    shape: it neither triggers a rewrite nor gets reset back to enabled."""
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    ensure_mngr_settings(MindsRoot("minds-dev-tname"))
    set_provider_is_enabled("modal", False, root=MindsRoot("minds-dev-tname"))

    assert ensure_mngr_settings(MindsRoot("minds-dev-tname")) is False
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["modal"]["is_enabled"] is False
    assert parsed["providers"]["modal"]["mode"] == "DIRECT"
    assert parsed["providers"]["modal"]["is_persistent"] is True


def test_ensure_mngr_settings_preserves_modal_is_enabled_on_rewrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When something else forces a rewrite, a panel-toggled modal Disable is carried
    over while the minds-controlled fields are re-pinned."""
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    ensure_mngr_settings(MindsRoot("minds-dev-tname"))
    set_provider_is_enabled("modal", False, root=MindsRoot("minds-dev-tname"))

    # Force the rewrite path: a stale extra aws-* block makes the desired-shape
    # check fail, so every minds-controlled block is re-pinned.
    with settings_path.open("a") as f:
        f.write('\n[providers.aws-eu-central-9]\nbackend = "aws"\n')

    assert ensure_mngr_settings(MindsRoot("minds-dev-tname")) is True
    parsed = tomllib.loads(settings_path.read_text())
    modal_block = parsed["providers"]["modal"]
    assert modal_block["is_enabled"] is False
    assert modal_block["mode"] == "DIRECT"
    assert modal_block["is_persistent"] is True


def test_ensure_mngr_settings_pins_modal_fields_around_legacy_panel_only_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A legacy ``[providers.modal]`` holding only a panel-written ``is_enabled = false``
    (from before the Modal-Direct block existed) gets the pinned fields added while the
    user's Disable is preserved."""
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    settings_path.write_text("[providers.modal]\nis_enabled = false\n")

    assert ensure_mngr_settings(MindsRoot("minds-dev-tname")) is True
    parsed = tomllib.loads(settings_path.read_text())
    modal_block = parsed["providers"]["modal"]
    assert modal_block["is_enabled"] is False
    assert modal_block["mode"] == "DIRECT"
    assert modal_block["is_persistent"] is True
