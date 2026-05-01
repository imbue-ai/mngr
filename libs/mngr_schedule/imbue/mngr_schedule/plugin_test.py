"""Unit tests for the mngr-schedule plugin registration."""

from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import click
import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_schedule.plugin import get_files_for_deploy
from imbue.mngr_schedule.plugin import modify_env_vars_for_deploy
from imbue.mngr_schedule.plugin import register_cli_commands


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override HOME for a test that relies on ``Path.home()`` pointing at
    an empty directory.

    The autouse ``setup_test_mngr_env`` fixture (in plugin_testing.py) sets
    HOME to ``tmp_path`` and writes ``~/.mngr/config.toml`` into it via
    ``register_test_sleep_agent_type``. Tests that assert on the contents
    of ``~/.mngr`` therefore need their own clean HOME, not the one the
    autouse fixture shares with ``host_dir``.
    """
    home = tmp_path / "isolated_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_register_cli_commands_returns_schedule_command() -> None:
    """Verify that register_cli_commands returns the schedule command."""
    result = register_cli_commands()

    assert result is not None
    assert isinstance(result, Sequence)
    assert len(result) == 1
    assert isinstance(result[0], click.Command)
    assert result[0].name == "schedule"


# =============================================================================
# get_files_for_deploy Tests
# =============================================================================


def _make_mngr_ctx_with_profile(profile_dir: Path, default_host_dir: Path) -> MngrContext:
    """Create a lightweight MngrContext stand-in with profile_dir + config.default_host_dir attrs."""
    config = SimpleNamespace(default_host_dir=default_host_dir)
    return cast(MngrContext, SimpleNamespace(profile_dir=profile_dir, config=config))


def test_get_files_for_deploy_returns_empty_dict_when_no_mngr_files(tmp_path: Path, isolated_home: Path) -> None:
    """get_files_for_deploy returns empty dict when no mngr config files exist."""
    mngr_ctx = _make_mngr_ctx_with_profile(tmp_path / "nonexistent-profile", Path.home() / ".mngr")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = get_files_for_deploy(
        mngr_ctx=mngr_ctx, include_user_settings=True, include_project_settings=True, repo_root=repo_root
    )

    assert result == {}


def test_get_files_for_deploy_includes_mngr_config(tmp_path: Path, isolated_home: Path) -> None:
    """get_files_for_deploy includes ~/.mngr/config.toml when it exists."""
    mngr_dir = isolated_home / ".mngr"
    mngr_dir.mkdir(parents=True, exist_ok=True)
    config_file = mngr_dir / "config.toml"
    config_file.write_text("[test]\nkey = 'value'\n")
    mngr_ctx = _make_mngr_ctx_with_profile(tmp_path / "nonexistent-profile", mngr_dir)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = get_files_for_deploy(
        mngr_ctx=mngr_ctx, include_user_settings=True, include_project_settings=True, repo_root=repo_root
    )

    assert Path("~/.mngr/config.toml") in result
    assert result[Path("~/.mngr/config.toml")] == config_file


def test_get_files_for_deploy_reads_config_from_deployer_host_dir(tmp_path: Path, isolated_home: Path) -> None:
    """When the deployer has a non-default MNGR_ROOT_NAME (e.g. minds, mngr-changelog-schedule),
    the config.toml lives at `~/.{root_name}/config.toml`, NOT `~/.mngr/config.toml`.
    get_files_for_deploy must read from mngr_ctx.config.default_host_dir, not hardcoded `~/.mngr`."""
    custom_host_dir = isolated_home / ".mngr-changelog-schedule"
    custom_host_dir.mkdir(parents=True, exist_ok=True)
    config_file = custom_host_dir / "config.toml"
    config_file.write_text('profile = "abc123"\n')
    mngr_ctx = _make_mngr_ctx_with_profile(tmp_path / "nonexistent-profile", custom_host_dir)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = get_files_for_deploy(
        mngr_ctx=mngr_ctx, include_user_settings=True, include_project_settings=True, repo_root=repo_root
    )

    # Staged at the deployer's actual path, not the hardcoded ~/.mngr/
    assert Path("~/.mngr-changelog-schedule/config.toml") in result
    assert result[Path("~/.mngr-changelog-schedule/config.toml")] == config_file


def test_get_files_for_deploy_includes_top_level_profile_files(tmp_path: Path, isolated_home: Path) -> None:
    """get_files_for_deploy includes top-level files from the profile directory."""
    profile_dir = isolated_home / ".mngr" / "profiles" / "test-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    settings_file = profile_dir / "settings.toml"
    settings_file.write_text("[test]\nvalue = 1\n")
    user_id_file = profile_dir / "user_id"
    user_id_file.write_text("abc123")
    mngr_ctx = _make_mngr_ctx_with_profile(profile_dir, Path.home() / ".mngr")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = get_files_for_deploy(
        mngr_ctx=mngr_ctx, include_user_settings=True, include_project_settings=True, repo_root=repo_root
    )

    assert Path("~/.mngr/profiles/test-profile/settings.toml") in result
    assert result[Path("~/.mngr/profiles/test-profile/settings.toml")] == settings_file
    assert Path("~/.mngr/profiles/test-profile/user_id") in result
    assert result[Path("~/.mngr/profiles/test-profile/user_id")] == user_id_file


def test_get_files_for_deploy_excludes_provider_subdirectories(tmp_path: Path, isolated_home: Path) -> None:
    """get_files_for_deploy does not include files from provider subdirectories."""
    profile_dir = isolated_home / ".mngr" / "profiles" / "test-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    # Create a top-level file that should be included
    (profile_dir / "settings.toml").write_text("[settings]")
    # Create a provider subdirectory with files that should NOT be included
    provider_dir = profile_dir / "providers" / "modal"
    provider_dir.mkdir(parents=True, exist_ok=True)
    (provider_dir / "modal_ssh_key").write_text("private-key")
    mngr_ctx = _make_mngr_ctx_with_profile(profile_dir, Path.home() / ".mngr")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = get_files_for_deploy(
        mngr_ctx=mngr_ctx, include_user_settings=True, include_project_settings=True, repo_root=repo_root
    )

    # Top-level profile file should be included
    assert Path("~/.mngr/profiles/test-profile/settings.toml") in result
    # Provider subdirectory files should NOT be included (handled by provider plugins)
    assert not any("providers" in str(k) for k in result)


def test_get_files_for_deploy_returns_empty_when_user_settings_excluded(tmp_path: Path, isolated_home: Path) -> None:
    """get_files_for_deploy returns empty dict when include_user_settings is False."""
    mngr_dir = isolated_home / ".mngr"
    mngr_dir.mkdir(parents=True, exist_ok=True)
    config_file = mngr_dir / "config.toml"
    config_file.write_text("[test]\nkey = 'value'\n")
    mngr_ctx = _make_mngr_ctx_with_profile(tmp_path / "nonexistent-profile", mngr_dir)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = get_files_for_deploy(
        mngr_ctx=mngr_ctx, include_user_settings=False, include_project_settings=True, repo_root=repo_root
    )

    # User settings excluded, but project settings has no .mngr/settings.local.toml
    assert result == {}


def test_get_files_for_deploy_includes_project_local_settings(tmp_path: Path) -> None:
    """get_files_for_deploy includes .mngr/settings.local.toml from the repo root."""
    mngr_ctx = _make_mngr_ctx_with_profile(tmp_path / "nonexistent-profile", Path.home() / ".mngr")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    local_mngr_dir = repo_root / ".mngr"
    local_mngr_dir.mkdir()
    local_config = local_mngr_dir / "settings.local.toml"
    local_config.write_text("[local]\noverride = true\n")

    result = get_files_for_deploy(
        mngr_ctx=mngr_ctx, include_user_settings=False, include_project_settings=True, repo_root=repo_root
    )

    assert Path(".mngr/settings.local.toml") in result
    assert result[Path(".mngr/settings.local.toml")] == local_config


def test_get_files_for_deploy_excludes_project_settings_when_flag_false(tmp_path: Path) -> None:
    """get_files_for_deploy skips project-local settings when include_project_settings is False."""
    mngr_ctx = _make_mngr_ctx_with_profile(tmp_path / "nonexistent-profile", Path.home() / ".mngr")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    local_mngr_dir = repo_root / ".mngr"
    local_mngr_dir.mkdir()
    (local_mngr_dir / "settings.local.toml").write_text("[local]\noverride = true\n")

    result = get_files_for_deploy(
        mngr_ctx=mngr_ctx, include_user_settings=False, include_project_settings=False, repo_root=repo_root
    )

    assert result == {}


# =============================================================================
# modify_env_vars_for_deploy Tests
# =============================================================================


def test_modify_env_vars_for_deploy_propagates_non_default_root_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the deployer has a non-default MNGR_ROOT_NAME, the hook propagates it
    so the scheduled container looks at the right `~/.<root_name>/` path and finds
    the files baked there by get_files_for_deploy."""
    monkeypatch.setenv("MNGR_ROOT_NAME", "mngr-changelog-schedule")
    mngr_ctx = cast(MngrContext, SimpleNamespace())
    env_vars: dict[str, str] = {}

    modify_env_vars_for_deploy(mngr_ctx=mngr_ctx, env_vars=env_vars)

    assert env_vars["MNGR_ROOT_NAME"] == "mngr-changelog-schedule"


def test_modify_env_vars_for_deploy_noop_for_default_root_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the deployer uses the default root name (no MNGR_ROOT_NAME set), the
    container's default also resolves to "mngr" and baked files already land at
    ~/.mngr/, so no propagation is needed."""
    monkeypatch.delenv("MNGR_ROOT_NAME", raising=False)
    mngr_ctx = cast(MngrContext, SimpleNamespace())
    env_vars: dict[str, str] = {}

    modify_env_vars_for_deploy(mngr_ctx=mngr_ctx, env_vars=env_vars)

    assert "MNGR_ROOT_NAME" not in env_vars


def test_modify_env_vars_for_deploy_respects_pre_existing_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit --pass-env / --env-file MNGR_ROOT_NAME wins so a caller can
    redirect a scheduled trigger to a different root_name if they really mean to."""
    monkeypatch.setenv("MNGR_ROOT_NAME", "deployer-root")
    mngr_ctx = cast(MngrContext, SimpleNamespace())
    env_vars: dict[str, str] = {"MNGR_ROOT_NAME": "override-root"}

    modify_env_vars_for_deploy(mngr_ctx=mngr_ctx, env_vars=env_vars)

    assert env_vars["MNGR_ROOT_NAME"] == "override-root"


def test_modify_env_vars_for_deploy_preserves_unrelated_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hook must not touch env vars unrelated to the mngr root_name anchor."""
    monkeypatch.setenv("MNGR_ROOT_NAME", "mngr")
    mngr_ctx = cast(MngrContext, SimpleNamespace())
    env_vars: dict[str, str] = {"GH_TOKEN": "ghp_xxx", "CUSTOM_VAR": "value"}

    modify_env_vars_for_deploy(mngr_ctx=mngr_ctx, env_vars=env_vars)

    assert env_vars["GH_TOKEN"] == "ghp_xxx"
    assert env_vars["CUSTOM_VAR"] == "value"
