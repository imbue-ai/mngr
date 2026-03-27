"""Unit tests for the mngr-schedule plugin registration."""

from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import click

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_schedule.plugin import get_files_for_deploy
from imbue.mngr_schedule.plugin import register_cli_commands


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


def _make_mngr_ctx_with_profile(profile_dir: Path) -> MngrContext:
    """Create a lightweight MngrContext stand-in with a profile_dir attribute."""
    return cast(MngrContext, SimpleNamespace(profile_dir=profile_dir))


def test_get_files_for_deploy_returns_empty_dict_when_no_mngr_files(tmp_path: Path) -> None:
    """get_files_for_deploy returns empty dict when no mngr config files exist."""
    mngr_ctx = _make_mngr_ctx_with_profile(tmp_path / "nonexistent-profile")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = get_files_for_deploy(
        mngr_ctx=mngr_ctx, include_user_settings=True, include_project_settings=True, repo_root=repo_root
    )

    assert result == {}


def test_get_files_for_deploy_includes_mngr_config(tmp_path: Path) -> None:
    """get_files_for_deploy includes ~/.mngr/config.toml when it exists."""
    mngr_dir = Path.home() / ".mngr"
    mngr_dir.mkdir(parents=True, exist_ok=True)
    config_file = mngr_dir / "config.toml"
    config_file.write_text("[test]\nkey = 'value'\n")
    mngr_ctx = _make_mngr_ctx_with_profile(tmp_path / "nonexistent-profile")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = get_files_for_deploy(
        mngr_ctx=mngr_ctx, include_user_settings=True, include_project_settings=True, repo_root=repo_root
    )

    assert Path("~/.mngr/config.toml") in result
    assert result[Path("~/.mngr/config.toml")] == config_file


def test_get_files_for_deploy_includes_top_level_profile_files(tmp_path: Path) -> None:
    """get_files_for_deploy includes top-level files from the profile directory."""
    profile_dir = Path.home() / ".mngr" / "profiles" / "test-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    settings_file = profile_dir / "settings.toml"
    settings_file.write_text("[test]\nvalue = 1\n")
    user_id_file = profile_dir / "user_id"
    user_id_file.write_text("abc123")
    mngr_ctx = _make_mngr_ctx_with_profile(profile_dir)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = get_files_for_deploy(
        mngr_ctx=mngr_ctx, include_user_settings=True, include_project_settings=True, repo_root=repo_root
    )

    assert Path("~/.mngr/profiles/test-profile/settings.toml") in result
    assert result[Path("~/.mngr/profiles/test-profile/settings.toml")] == settings_file
    assert Path("~/.mngr/profiles/test-profile/user_id") in result
    assert result[Path("~/.mngr/profiles/test-profile/user_id")] == user_id_file


def test_get_files_for_deploy_excludes_provider_subdirectories(tmp_path: Path) -> None:
    """get_files_for_deploy does not include files from provider subdirectories."""
    profile_dir = Path.home() / ".mngr" / "profiles" / "test-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    # Create a top-level file that should be included
    (profile_dir / "settings.toml").write_text("[settings]")
    # Create a provider subdirectory with files that should NOT be included
    provider_dir = profile_dir / "providers" / "modal"
    provider_dir.mkdir(parents=True, exist_ok=True)
    (provider_dir / "modal_ssh_key").write_text("private-key")
    mngr_ctx = _make_mngr_ctx_with_profile(profile_dir)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = get_files_for_deploy(
        mngr_ctx=mngr_ctx, include_user_settings=True, include_project_settings=True, repo_root=repo_root
    )

    # Top-level profile file should be included
    assert Path("~/.mngr/profiles/test-profile/settings.toml") in result
    # Provider subdirectory files should NOT be included (handled by provider plugins)
    assert not any("providers" in str(k) for k in result)


def test_get_files_for_deploy_returns_empty_when_user_settings_excluded(tmp_path: Path) -> None:
    """get_files_for_deploy returns empty dict when include_user_settings is False."""
    mngr_dir = Path.home() / ".mngr"
    mngr_dir.mkdir(parents=True, exist_ok=True)
    config_file = mngr_dir / "config.toml"
    config_file.write_text("[test]\nkey = 'value'\n")
    mngr_ctx = _make_mngr_ctx_with_profile(tmp_path / "nonexistent-profile")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = get_files_for_deploy(
        mngr_ctx=mngr_ctx, include_user_settings=False, include_project_settings=True, repo_root=repo_root
    )

    # User settings excluded, but project settings has no .mngr/settings.local.toml
    assert result == {}


def test_get_files_for_deploy_includes_project_local_settings(tmp_path: Path) -> None:
    """get_files_for_deploy includes .mngr/settings.local.toml from the repo root."""
    mngr_ctx = _make_mngr_ctx_with_profile(tmp_path / "nonexistent-profile")
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
    mngr_ctx = _make_mngr_ctx_with_profile(tmp_path / "nonexistent-profile")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    local_mngr_dir = repo_root / ".mngr"
    local_mngr_dir.mkdir()
    (local_mngr_dir / "settings.local.toml").write_text("[local]\noverride = true\n")

    result = get_files_for_deploy(
        mngr_ctx=mngr_ctx, include_user_settings=False, include_project_settings=False, repo_root=repo_root
    )

    assert result == {}
