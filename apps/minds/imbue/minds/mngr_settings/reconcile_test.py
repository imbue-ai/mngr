import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import MindsRoot
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


def test_importing_the_pre_import_reconcile_loads_no_mngr_module() -> None:
    """``main.py`` imports the reconcile and then refuses to run if any ``imbue.mngr`` module is loaded.

    The in-process tests below forget mngr modules first, so only a fresh interpreter can show that
    the reconcile's own imports keep mngr out.
    """
    probe = (
        "import sys\n"
        "import imbue.minds.mngr_settings.reconcile\n"
        "print(sorted(name for name in sys.modules if name == 'imbue.mngr' or name.startswith('imbue.mngr.')))\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"


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
    the per-account ``imbue_cloud_<user id>`` entries.
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
    AWS providers are the ``aws_<hex>`` connection instances.
    """
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    ensure_mngr_settings(MindsRoot("minds-dev-tname"))
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["aws"] == {"backend": "aws", "is_enabled": False}


def test_ensure_mngr_settings_removes_legacy_ambient_aws_region_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ambient ``aws-<region>`` blocks written by earlier builds are actively
    deleted at boot (the machine-credential AWS path was removed from minds).
    Other blocks survive this pre-import pass (the materializer owns them)."""
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


def test_ensure_mngr_settings_suppresses_the_default_modal_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Modal is a connection with its own credentials now, so the default instance is suppressed like the others."""
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    ensure_mngr_settings(MindsRoot("minds-dev-tname"))
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["modal"] == {"backend": "modal", "is_enabled": False}


def test_ensure_mngr_settings_rewrites_a_legacy_ambient_modal_direct_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ambient DIRECT block older builds wrote (with a panel-written enable) collapses to the suppression form."""
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    settings_path.write_text(
        '[providers.modal]\nbackend = "modal"\nmode = "DIRECT"\nis_enabled = true\nis_persistent = true\n'
    )

    assert ensure_mngr_settings(MindsRoot("minds-dev-tname")) is True
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["modal"] == {"backend": "modal", "is_enabled": False}
    assert ensure_mngr_settings(MindsRoot("minds-dev-tname")) is False


_PANEL_DISABLED_SETTINGS = "\n".join(
    [
        "[providers.docker]",
        "is_enabled = false",
        "[providers.lima]",
        'backend = "lima"',
        "is_enabled = false",
        "[providers.vultr]",
        'backend = "vultr"',
        "is_enabled = true",
        "[providers.aws]",
        'backend = "aws"',
        "is_enabled = false",
        "",
    ]
)


def test_ensure_clears_the_disabled_flag_the_retired_panel_toggle_wrote_exactly_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A docker/lima/vultr block disabled by the old toggle is re-enabled once; a later hand edit is left alone."""
    root_name = "minds-dev-tname"
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, root_name)
    settings_path.write_text(_PANEL_DISABLED_SETTINGS)
    root = MindsRoot(root_name)

    # The migration's write counts as a write, like the reconcile's own.
    assert ensure_mngr_settings(root) is True

    providers = tomllib.loads(settings_path.read_text())["providers"]
    # A block left empty by the removal goes with it; a block with other keys keeps them.
    assert "docker" not in providers
    assert providers["lima"] == {"backend": "lima"}
    assert providers["vultr"] == {"backend": "vultr", "is_enabled": True}
    # The suppressed defaults are the reconcile's own and stay disabled.
    assert providers["aws"] == {"backend": "aws", "is_enabled": False}
    marker_path = root.state_dir / "migrations" / "panel_provider_disables_cleared"
    assert marker_path.is_file()

    # Done once: a deliberate disable written afterwards survives the next reconcile.
    settings_path.write_text(settings_path.read_text() + "[providers.docker]\nis_enabled = false\n")
    assert ensure_mngr_settings(root) is False
    assert tomllib.loads(settings_path.read_text())["providers"]["docker"] == {"is_enabled": False}


def test_the_one_shot_migration_retries_until_its_save_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The marker is written only after a successful save, so a save that raised is tried again."""
    root_name = "minds-dev-tname"
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, root_name)
    settings_path.write_text(_PANEL_DISABLED_SETTINGS)
    root = MindsRoot(root_name)
    marker_path = root.state_dir / "migrations" / "panel_provider_disables_cleared"
    # A directory where the store's temp file goes makes the save fail whoever runs the
    # test (a permission bit would not stop root, which CI runs as).
    blocking_dir = settings_path.with_suffix(".tmp")
    blocking_dir.mkdir()
    with pytest.raises(OSError):
        ensure_mngr_settings(root)
    blocking_dir.rmdir()
    assert not marker_path.exists()

    ensure_mngr_settings(root)

    assert marker_path.is_file()
    assert "docker" not in tomllib.loads(settings_path.read_text())["providers"]
