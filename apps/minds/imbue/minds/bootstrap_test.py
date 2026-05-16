import os
import re
import tomllib
from pathlib import Path

import pytest

from imbue.minds.bootstrap import BootstrapError
from imbue.minds.bootstrap import DEFAULT_MINDS_ROOT_NAME
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import MINDS_ROOT_NAME_PATTERN
from imbue.minds.bootstrap import _ensure_mngr_settings
from imbue.minds.bootstrap import apply_bootstrap
from imbue.minds.bootstrap import disable_imbue_cloud_provider_for_account
from imbue.minds.bootstrap import env_name_from_root_name
from imbue.minds.bootstrap import is_minds_root_name_set_to_active_env
from imbue.minds.bootstrap import minds_data_dir_for
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.bootstrap import mngr_prefix_for
from imbue.minds.bootstrap import resolve_minds_root_name
from imbue.minds.bootstrap import root_name_for_env_name
from imbue.minds.bootstrap import set_imbue_cloud_provider_for_account


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove MINDS_ROOT_NAME and MNGR_* overrides that tests might have set."""
    monkeypatch.delenv(MINDS_ROOT_NAME_ENV_VAR, raising=False)
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    monkeypatch.delenv("MNGR_PREFIX", raising=False)


def test_defaults_to_minds_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    assert resolve_minds_root_name() == DEFAULT_MINDS_ROOT_NAME


def test_accepts_minds_value_for_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds")
    assert resolve_minds_root_name() == "minds"


def test_accepts_minds_prefix_for_dev_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-josh-3")
    assert resolve_minds_root_name() == "minds-josh-3"


def test_accepts_minds_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-staging")
    assert resolve_minds_root_name() == "minds-staging"


def test_legacy_devminds_value_falls_back_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale `MINDS_ROOT_NAME=devminds` parent shell shouldn't break us.

    Per the per-env-data-roots refactor: values that don't match the
    `minds(-<env-name>)?` pattern are silently treated as unset and the
    caller falls back to production (~/.minds/). The Python warning
    surfaces in logs so the operator notices.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "devminds")
    # No SystemExit -- just fall back to the default.
    assert resolve_minds_root_name() == DEFAULT_MINDS_ROOT_NAME


def test_legacy_value_with_spaces_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "Has Spaces")
    assert resolve_minds_root_name() == DEFAULT_MINDS_ROOT_NAME


def test_path_with_dot_dot_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "../evil")
    # ../evil cannot match `minds(-<env-name>)?` -- treated as unset.
    assert resolve_minds_root_name() == DEFAULT_MINDS_ROOT_NAME


def test_is_active_when_set_to_valid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-josh-3")
    assert is_minds_root_name_set_to_active_env() is True


def test_is_active_when_set_to_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds")
    assert is_minds_root_name_set_to_active_env() is True


def test_is_active_false_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    assert is_minds_root_name_set_to_active_env() is False


def test_is_active_false_for_legacy_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale shell with `MINDS_ROOT_NAME=devminds` is NOT activated.

    `minds env deploy` / `destroy` use this distinction to refuse safely
    even when the bootstrap fallback would still produce a usable host_dir.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "devminds")
    assert is_minds_root_name_set_to_active_env() is False


def test_env_name_from_root_name_production() -> None:
    assert env_name_from_root_name("minds") == "production"


def test_env_name_from_root_name_dev() -> None:
    assert env_name_from_root_name("minds-josh-3") == "josh-3"


def test_env_name_from_root_name_staging() -> None:
    assert env_name_from_root_name("minds-staging") == "staging"


def test_env_name_from_root_name_rejects_garbage() -> None:
    with pytest.raises(BootstrapError):
        env_name_from_root_name("devminds")


def test_root_name_for_env_name_production() -> None:
    assert root_name_for_env_name("production") == "minds"


def test_root_name_for_env_name_dev() -> None:
    assert root_name_for_env_name("josh-3") == "minds-josh-3"


def test_root_name_for_env_name_staging() -> None:
    assert root_name_for_env_name("staging") == "minds-staging"


def test_minds_data_dir_for() -> None:
    assert minds_data_dir_for("minds-josh-3") == Path.home() / ".minds-josh-3"
    assert minds_data_dir_for("minds") == Path.home() / ".minds"


def test_mngr_host_dir_for() -> None:
    assert mngr_host_dir_for("minds-josh-3") == Path.home() / ".minds-josh-3" / "mngr"


def test_mngr_prefix_for() -> None:
    assert mngr_prefix_for("minds-josh-3") == "minds-josh-3-"
    assert mngr_prefix_for("minds") == "minds-"


def test_apply_bootstrap_sets_env_vars_when_root_name_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-testname")
    apply_bootstrap()

    assert os.environ["MNGR_HOST_DIR"] == str(Path.home() / ".minds-testname" / "mngr")
    assert os.environ["MNGR_PREFIX"] == "minds-testname-"


def test_apply_bootstrap_overrides_inherited_mngr_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit MINDS_ROOT_NAME wins over an inherited MNGR_HOST_DIR/MNGR_PREFIX.

    Without this, a minds process spawned from a parent that already set
    MNGR_HOST_DIR (e.g. a Claude Code agent's tmux) would silently keep the
    parent's host_dir and read a different mngr settings.toml than the one
    minds bootstrap writes to.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-josh-3")
    monkeypatch.setenv("MNGR_HOST_DIR", "/custom/host/dir")
    monkeypatch.setenv("MNGR_PREFIX", "custom-")
    apply_bootstrap()

    assert os.environ["MNGR_HOST_DIR"] == str(Path.home() / ".minds-josh-3" / "mngr")
    assert os.environ["MNGR_PREFIX"] == "minds-josh-3-"


def test_apply_bootstrap_leaves_mngr_vars_alone_when_root_name_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-env-data-roots refactor: apply_bootstrap is a no-op when MINDS_ROOT_NAME is unset.

    Callers that need an activated env refuse explicitly. Callers that
    only need the production data dir handle it themselves.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv("MNGR_HOST_DIR", "/custom/host/dir")
    monkeypatch.setenv("MNGR_PREFIX", "custom-")
    apply_bootstrap()

    assert os.environ["MNGR_HOST_DIR"] == "/custom/host/dir"
    assert os.environ["MNGR_PREFIX"] == "custom-"


def test_apply_bootstrap_unset_does_not_write_mngr_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    apply_bootstrap()
    # Vars stay unset because there's no activated env to drive them.
    assert "MNGR_HOST_DIR" not in os.environ
    assert "MNGR_PREFIX" not in os.environ


def test_apply_bootstrap_invalid_value_still_writes_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale `MINDS_ROOT_NAME=devminds` shell still gets consistent MNGR_* vars.

    The bootstrap resolves to the production default and exports the
    derived vars so downstream mngr calls have *some* consistent
    host_dir to point at instead of half-honoring the bad value.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "devminds")
    monkeypatch.setenv("MNGR_HOST_DIR", "/custom/host/dir")
    apply_bootstrap()
    assert os.environ["MNGR_HOST_DIR"] == str(Path.home() / ".minds" / "mngr")
    assert os.environ["MNGR_PREFIX"] == "minds-"


def test_minds_root_name_pattern_canonical_examples() -> None:
    """Sanity-check the regex's expectations directly."""
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds") is not None
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds-staging") is not None
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds-josh-3") is not None
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "devminds") is None
    # Bare `minds-` with no suffix is rejected -- the env-name regex
    # forbids an empty suffix.
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds-") is None
    # Single-char env-name suffixes are rejected -- DEV_ENV_NAME_PATTERN
    # requires both a leading and a trailing alphanumeric (2+ chars).
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds-a") is None


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


_FAKE_CONNECTOR_URL = "https://test--remote-service-connector-fastapi-app.modal.run"


def test_set_imbue_cloud_provider_for_account_writes_block(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings_path = _stub_mngr_host_dir(monkeypatch, tmp_path, "minds-tname")
    changed = set_imbue_cloud_provider_for_account(
        "alice@example.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root_name="minds-tname",
    )
    assert changed is True
    parsed = tomllib.loads(settings_path.read_text())
    block = parsed["providers"]["imbue_cloud_alice-example-com"]
    assert block == {
        "backend": "imbue_cloud",
        "account": "alice@example.com",
        "connector_url": _FAKE_CONNECTOR_URL,
        "is_enabled": True,
    }


def test_disable_imbue_cloud_provider_for_account_flips_is_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings_path = _stub_mngr_host_dir(monkeypatch, tmp_path, "minds-tname")
    set_imbue_cloud_provider_for_account(
        "alice@example.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root_name="minds-tname",
    )

    changed = disable_imbue_cloud_provider_for_account("alice@example.com", root_name="minds-tname")
    assert changed is True
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud_alice-example-com"]["is_enabled"] is False

    # Idempotent: a second disable is a no-op.
    assert disable_imbue_cloud_provider_for_account("alice@example.com", root_name="minds-tname") is False


def test_set_force_enable_re_enables_disabled_block(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings_path = _stub_mngr_host_dir(monkeypatch, tmp_path, "minds-tname")
    set_imbue_cloud_provider_for_account(
        "alice@example.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root_name="minds-tname",
    )
    disable_imbue_cloud_provider_for_account("alice@example.com", root_name="minds-tname")

    changed = set_imbue_cloud_provider_for_account(
        "alice@example.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root_name="minds-tname",
        force_enable=True,
    )
    assert changed is True
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud_alice-example-com"]["is_enabled"] is True


def test_set_preserve_does_not_re_enable_disabled_block(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The bootstrap reconcile path must leave a previously auto-disabled
    provider disabled -- only an explicit signin event force-enables.
    """
    settings_path = _stub_mngr_host_dir(monkeypatch, tmp_path, "minds-tname")
    set_imbue_cloud_provider_for_account(
        "alice@example.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root_name="minds-tname",
    )
    disable_imbue_cloud_provider_for_account("alice@example.com", root_name="minds-tname")

    changed = set_imbue_cloud_provider_for_account(
        "alice@example.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root_name="minds-tname",
        force_enable=False,
    )
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
    settings_path = _stub_mngr_host_dir(monkeypatch, tmp_path, "minds-tname")
    _ensure_mngr_settings("minds-tname")
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud"] == {"backend": "imbue_cloud", "is_enabled": False}
    assert parsed["plugins"]["recursive"]["enabled"] is False
