import os
from pathlib import Path

import pytest

from imbue.minds.bootstrap import DEFAULT_MINDS_ROOT_NAME
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import apply_bootstrap
from imbue.minds.bootstrap import minds_data_dir_for
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.bootstrap import mngr_prefix_for
from imbue.minds.bootstrap import resolve_minds_root_name


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


def test_apply_bootstrap_respects_existing_mngr_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """If MNGR_HOST_DIR/MNGR_PREFIX already set, bootstrap does not overwrite them.

    This lets advanced users and test fixtures pin the mngr-layer vars independently
    of MINDS_ROOT_NAME.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds")
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
