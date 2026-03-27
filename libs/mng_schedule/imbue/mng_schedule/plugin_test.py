"""Unit tests for the mng-schedule plugin registration."""

from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import click

from imbue.mng.config.data_types import MngContext
from imbue.mng_schedule.plugin import get_files_for_deploy
from imbue.mng_schedule.plugin import register_cli_commands


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


def _make_mng_ctx_with_profile(profile_dir: Path) -> MngContext:
    """Create a lightweight MngContext stand-in with a profile_dir attribute."""
    return cast(MngContext, SimpleNamespace(profile_dir=profile_dir))


def test_get_files_for_deploy_returns_empty_dict_when_no_mng_files(tmp_path: Path) -> None:
    """get_files_for_deploy returns empty dict when no mng config files exist."""
    mng_ctx = _make_mng_ctx_with_profile(tmp_path / "nonexistent-profile")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = get_files_for_deploy(
        mng_ctx=mng_ctx, include_user_settings=True, include_project_settings=True, repo_root=repo_root
    )

    assert result == {}


def test_get_files_for_deploy_includes_mng_config(tmp_path: Path) -> None:
    """get_files_for_deploy includes ~/.mng/config.toml when it exists."""
    mng_dir = Path.home() / ".mng"
    mng_dir.mkdir(parents=True, exist_ok=True)
    config_file = mng_dir / "config.toml"
    config_file.write_text("[test]\nkey = 'value'\n")
    mng_ctx = _make_mng_ctx_with_profile(tmp_path / "nonexistent-profile")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = get_files_for_deploy(
        mng_ctx=mng_ctx, include_user_settings=True, include_project_settings=True, repo_root=repo_root
    )

    assert Path("~/.mng/config.toml") in result
    assert result[Path("~/.mng/config.toml")] == config_file


def test_get_files_for_deploy_includes_top_level_profile_files(tmp_path: Path) -> None:
    """get_files_for_deploy includes top-level files from the profile directory."""
    profile_dir = Path.home() / ".mng" / "profiles" / "test-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    settings_file = profile_dir / "settings.toml"
    settings_file.write_text("[test]\nvalue = 1\n")
    user_id_file = profile_dir / "user_id"
    user_id_file.write_text("abc123")
    mng_ctx = _make_mng_ctx_with_profile(profile_dir)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = get_files_for_deploy(
        mng_ctx=mng_ctx, include_user_settings=True, include_project_settings=True, repo_root=repo_root
    )

    assert Path("~/.mng/profiles/test-profile/settings.toml") in result
    assert result[Path("~/.mng/profiles/test-profile/settings.toml")] == settings_file
    assert Path("~/.mng/profiles/test-profile/user_id") in result
    assert result[Path("~/.mng/profiles/test-profile/user_id")] == user_id_file


def test_get_files_for_deploy_excludes_provider_subdirectories(tmp_path: Path) -> None:
    """get_files_for_deploy does not include files from provider subdirectories."""
    profile_dir = Path.home() / ".mng" / "profiles" / "test-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    # Create a top-level file that should be included
    (profile_dir / "settings.toml").write_text("[settings]")
    # Create a provider subdirectory with files that should NOT be included
    provider_dir = profile_dir / "providers" / "modal"
    provider_dir.mkdir(parents=True, exist_ok=True)
    (provider_dir / "modal_ssh_key").write_text("private-key")
    mng_ctx = _make_mng_ctx_with_profile(profile_dir)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = get_files_for_deploy(
        mng_ctx=mng_ctx, include_user_settings=True, include_project_settings=True, repo_root=repo_root
    )

    # Top-level profile file should be included
    assert Path("~/.mng/profiles/test-profile/settings.toml") in result
    # Provider subdirectory files should NOT be included (handled by provider plugins)
    assert not any("providers" in str(k) for k in result)


def test_get_files_for_deploy_returns_empty_when_user_settings_excluded(tmp_path: Path) -> None:
    """get_files_for_deploy returns empty dict when include_user_settings is False."""
    mng_dir = Path.home() / ".mng"
    mng_dir.mkdir(parents=True, exist_ok=True)
    config_file = mng_dir / "config.toml"
    config_file.write_text("[test]\nkey = 'value'\n")
    mng_ctx = _make_mng_ctx_with_profile(tmp_path / "nonexistent-profile")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = get_files_for_deploy(
        mng_ctx=mng_ctx, include_user_settings=False, include_project_settings=True, repo_root=repo_root
    )

    # User settings excluded, but project settings has no .mng/settings.local.toml
    assert result == {}


def test_get_files_for_deploy_includes_project_local_settings(tmp_path: Path) -> None:
    """get_files_for_deploy includes .mng/settings.local.toml from the repo root."""
    mng_ctx = _make_mng_ctx_with_profile(tmp_path / "nonexistent-profile")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    local_mng_dir = repo_root / ".mng"
    local_mng_dir.mkdir()
    local_config = local_mng_dir / "settings.local.toml"
    local_config.write_text("[local]\noverride = true\n")

    result = get_files_for_deploy(
        mng_ctx=mng_ctx, include_user_settings=False, include_project_settings=True, repo_root=repo_root
    )

    assert Path(".mng/settings.local.toml") in result
    assert result[Path(".mng/settings.local.toml")] == local_config


def test_get_files_for_deploy_excludes_project_settings_when_flag_false(tmp_path: Path) -> None:
    """get_files_for_deploy skips project-local settings when include_project_settings is False."""
    mng_ctx = _make_mng_ctx_with_profile(tmp_path / "nonexistent-profile")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    local_mng_dir = repo_root / ".mng"
    local_mng_dir.mkdir()
    (local_mng_dir / "settings.local.toml").write_text("[local]\noverride = true\n")

    result = get_files_for_deploy(
        mng_ctx=mng_ctx, include_user_settings=False, include_project_settings=False, repo_root=repo_root
    )

    assert result == {}
