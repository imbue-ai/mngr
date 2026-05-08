import os
import tomllib
from pathlib import Path

import pytest

from imbue.minds.bootstrap import DEFAULT_MINDS_ROOT_NAME
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import _ensure_mngr_settings
from imbue.minds.bootstrap import apply_bootstrap
from imbue.minds.bootstrap import disable_imbue_cloud_provider_for_account
from imbue.minds.bootstrap import minds_data_dir_for
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.bootstrap import mngr_prefix_for
from imbue.minds.bootstrap import resolve_minds_root_name
from imbue.minds.bootstrap import set_imbue_cloud_provider_for_account


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove MINDS_ROOT_NAME and MNGR_* overrides that tests might have set."""
    monkeypatch.delenv(MINDS_ROOT_NAME_ENV_VAR, raising=False)
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    monkeypatch.delenv("MNGR_PREFIX", raising=False)


def test_defaults_to_minds_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    assert resolve_minds_root_name() == DEFAULT_MINDS_ROOT_NAME


def test_reads_minds_root_name_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "devminds")
    assert resolve_minds_root_name() == "devminds"


def test_invalid_minds_root_name_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "Has Spaces")
    with pytest.raises(SystemExit) as excinfo:
        resolve_minds_root_name()
    assert excinfo.value.code == 1


def test_path_with_dot_dot_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "../evil")
    with pytest.raises(SystemExit):
        resolve_minds_root_name()


def test_minds_data_dir_for() -> None:
    result = minds_data_dir_for("devminds")
    assert result == Path.home() / ".devminds"


def test_mngr_host_dir_for() -> None:
    result = mngr_host_dir_for("devminds")
    assert result == Path.home() / ".devminds" / "mngr"


def test_mngr_prefix_for() -> None:
    assert mngr_prefix_for("devminds") == "devminds-"
    assert mngr_prefix_for("minds") == "minds-"


def test_apply_bootstrap_sets_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "testname")
    apply_bootstrap()

    assert os.environ["MNGR_HOST_DIR"] == str(Path.home() / ".testname" / "mngr")
    assert os.environ["MNGR_PREFIX"] == "testname-"


def test_apply_bootstrap_overrides_existing_mngr_vars_when_root_name_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit MINDS_ROOT_NAME wins over an inherited MNGR_HOST_DIR/MNGR_PREFIX.

    Without this, a minds process spawned from a parent that already set
    MNGR_HOST_DIR (e.g. a Claude Code agent's tmux) would silently keep the
    parent's host_dir and read a different mngr settings.toml than the one
    minds bootstrap writes to.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "devminds")
    monkeypatch.setenv("MNGR_HOST_DIR", "/custom/host/dir")
    monkeypatch.setenv("MNGR_PREFIX", "custom-")
    apply_bootstrap()

    assert os.environ["MNGR_HOST_DIR"] == str(Path.home() / ".devminds" / "mngr")
    assert os.environ["MNGR_PREFIX"] == "devminds-"


def test_apply_bootstrap_respects_existing_mngr_vars_when_root_name_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When MINDS_ROOT_NAME is not explicitly set, existing MNGR vars are preserved.

    Lets test fixtures and advanced users who pin MNGR_HOST_DIR directly
    keep doing so without having to also unset MINDS_ROOT_NAME.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv("MNGR_HOST_DIR", "/custom/host/dir")
    monkeypatch.setenv("MNGR_PREFIX", "custom-")
    apply_bootstrap()

    assert os.environ["MNGR_HOST_DIR"] == "/custom/host/dir"
    assert os.environ["MNGR_PREFIX"] == "custom-"


def test_apply_bootstrap_default_root_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    apply_bootstrap()

    assert os.environ["MNGR_HOST_DIR"] == str(Path.home() / ".minds" / "mngr")
    assert os.environ["MNGR_PREFIX"] == "minds-"


def _stub_mngr_host_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, root_name: str) -> Path:
    """Redirect ``Path.home()`` to ``tmp_path`` and seed a minimal mngr profile.

    Returns the active ``settings.toml`` path. The bootstrap helpers refuse
    to write anything until ``config.toml`` and the matching profile dir
    exist, so we materialize them up front. ``Path.home()`` consults
    ``$HOME`` on Linux/macOS, so swapping that in via monkeypatch.setenv
    is enough to redirect the helpers without touching ``Path`` itself.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    mngr_host_dir = mngr_host_dir_for(root_name)
    mngr_host_dir.mkdir(parents=True, exist_ok=True)
    profile_id = "testprofile"
    (mngr_host_dir / "config.toml").write_text(f'profile = "{profile_id}"\n')
    settings_dir = mngr_host_dir / "profiles" / profile_id
    settings_dir.mkdir(parents=True, exist_ok=True)
    return settings_dir / "settings.toml"


def test_set_imbue_cloud_provider_for_account_writes_block(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings_path = _stub_mngr_host_dir(monkeypatch, tmp_path, "tname")
    changed = set_imbue_cloud_provider_for_account("alice@example.com", root_name="tname")
    assert changed is True
    parsed = tomllib.loads(settings_path.read_text())
    block = parsed["providers"]["imbue_cloud_alice-example-com"]
    assert block == {"backend": "imbue_cloud", "account": "alice@example.com", "is_enabled": True}


def test_disable_imbue_cloud_provider_for_account_flips_is_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings_path = _stub_mngr_host_dir(monkeypatch, tmp_path, "tname")
    set_imbue_cloud_provider_for_account("alice@example.com", root_name="tname")

    changed = disable_imbue_cloud_provider_for_account("alice@example.com", root_name="tname")
    assert changed is True
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud_alice-example-com"]["is_enabled"] is False

    # Idempotent: a second disable is a no-op.
    assert disable_imbue_cloud_provider_for_account("alice@example.com", root_name="tname") is False


def test_set_force_enable_re_enables_disabled_block(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings_path = _stub_mngr_host_dir(monkeypatch, tmp_path, "tname")
    set_imbue_cloud_provider_for_account("alice@example.com", root_name="tname")
    disable_imbue_cloud_provider_for_account("alice@example.com", root_name="tname")

    changed = set_imbue_cloud_provider_for_account("alice@example.com", root_name="tname", force_enable=True)
    assert changed is True
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud_alice-example-com"]["is_enabled"] is True


def test_set_preserve_does_not_re_enable_disabled_block(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The bootstrap reconcile path must leave a previously auto-disabled
    provider disabled -- only an explicit signin event force-enables.
    """
    settings_path = _stub_mngr_host_dir(monkeypatch, tmp_path, "tname")
    set_imbue_cloud_provider_for_account("alice@example.com", root_name="tname")
    disable_imbue_cloud_provider_for_account("alice@example.com", root_name="tname")

    changed = set_imbue_cloud_provider_for_account("alice@example.com", root_name="tname", force_enable=False)
    assert changed is False
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud_alice-example-com"]["is_enabled"] is False


def test_ensure_mngr_settings_writes_default_imbue_cloud_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_ensure_mngr_settings`` must suppress the default ``[providers.imbue_cloud]``
    instance so ``get_all_provider_instances`` doesn't auto-create one alongside
    the per-account ``imbue_cloud_<slug>`` entries.
    """
    settings_path = _stub_mngr_host_dir(monkeypatch, tmp_path, "tname")
    _ensure_mngr_settings("tname")
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud"] == {"backend": "imbue_cloud", "is_enabled": False}
    assert parsed["plugins"]["recursive"]["enabled"] is False
