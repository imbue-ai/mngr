"""Unit tests for create_plugin_manager."""

from pathlib import Path

import pytest

from imbue.mngr.main import create_plugin_manager


def test_create_plugin_manager_blocks_disabled_plugins(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
) -> None:
    """create_plugin_manager should block plugins disabled in config files."""
    (project_config_dir / "settings.toml").write_text("[plugins.modal]\nenabled = false\n")

    pm = create_plugin_manager()

    assert pm.is_blocked("modal")


def test_create_plugin_manager_skips_blocking_when_load_all_plugins_set(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_plugin_manager should skip blocking when MNGR_LOAD_ALL_PLUGINS is truthy."""
    (project_config_dir / "settings.toml").write_text("[plugins.modal]\nenabled = false\n")
    monkeypatch.setenv("MNGR_LOAD_ALL_PLUGINS", "1")

    pm = create_plugin_manager()

    assert not pm.is_blocked("modal")
