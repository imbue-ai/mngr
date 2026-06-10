"""Unit tests for create_plugin_manager."""

import os
from pathlib import Path

import pytest

from imbue.mngr.main import create_plugin_manager
from imbue.mngr.utils.env_utils import parse_bool_env


def test_create_plugin_manager_blocks_disabled_plugins(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
) -> None:
    """create_plugin_manager should block plugins disabled in config files."""
    # MNGR_LOAD_ALL_PLUGINS disables config-based blocking, so if it is set it would
    # silently mask this test. It must never be set during a normal test run, so treat
    # its presence as a leak and fail loudly -- some other test or imported module set
    # it process-wide (e.g. importing scripts/make_cli_docs, which sets it at import
    # time and is expected to pop it again). Surface the leak so it gets fixed at the
    # source rather than papered over here.
    assert not parse_bool_env(os.environ.get("MNGR_LOAD_ALL_PLUGINS", "")), (
        "MNGR_LOAD_ALL_PLUGINS is set in the test environment, which disables plugin "
        "blocking and would mask this test. It leaked into the process from another "
        "test or an imported module (e.g. an importer of scripts/make_cli_docs that "
        "failed to pop it). Find and contain the leak at its source."
    )
    (project_config_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n[plugins.modal]\nenabled = false\n"
    )

    pm = create_plugin_manager()

    assert pm.is_blocked("modal")


def test_create_plugin_manager_skips_blocking_when_load_all_plugins_set(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_plugin_manager should skip blocking when MNGR_LOAD_ALL_PLUGINS is truthy."""
    (project_config_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n[plugins.modal]\nenabled = false\n"
    )
    monkeypatch.setenv("MNGR_LOAD_ALL_PLUGINS", "1")

    pm = create_plugin_manager()

    assert not pm.is_blocked("modal")
