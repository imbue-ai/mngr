"""Tests for config pre-readers."""

from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.pre_readers import get_local_config_name
from imbue.mngr.config.pre_readers import get_project_config_name
from imbue.mngr.config.pre_readers import get_user_config_path
from imbue.mngr.config.pre_readers import load_local_config
from imbue.mngr.config.pre_readers import load_project_config
from imbue.mngr.config.pre_readers import read_default_command
from imbue.mngr.config.pre_readers import read_disabled_plugins
from imbue.mngr.config.pre_readers import resolve_project_config_dir
from imbue.mngr.config.pre_readers import try_load_toml
from imbue.mngr.errors import ConfigParseError

# =============================================================================
# Tests for try_load_toml
# =============================================================================


def test_try_load_toml_returns_none_for_none_path() -> None:
    """try_load_toml should return None when given a None path."""
    assert try_load_toml(None) is None


def test_try_load_toml_returns_none_for_missing_file(tmp_path: Path) -> None:
    """try_load_toml should return None when the file does not exist."""
    assert try_load_toml(tmp_path / "nonexistent.toml") is None


def test_try_load_toml_raises_on_malformed_toml(tmp_path: Path) -> None:
    """try_load_toml should raise ConfigParseError when the file contains invalid TOML."""
    invalid_toml = tmp_path / "invalid.toml"
    invalid_toml.write_text("[invalid toml syntax")
    with pytest.raises(ConfigParseError, match="Failed to parse config file"):
        try_load_toml(invalid_toml)


def test_try_load_toml_parses_valid_file(tmp_path: Path) -> None:
    """try_load_toml should parse valid TOML files and return the dict."""
    valid_toml = tmp_path / "valid.toml"
    valid_toml.write_text('prefix = "test-"\n[agent_types.claude]\ncommand = "claude"')
    result = try_load_toml(valid_toml)
    assert result is not None
    assert result["prefix"] == "test-"
    assert result["agent_types"]["claude"]["command"] == "claude"


# =============================================================================
# Tests for config file path functions
# =============================================================================


def test_get_user_config_path_returns_correct_path() -> None:
    """get_user_config_path should return settings.toml in profile directory."""
    profile_dir = Path("/home/user/.mngr/profiles/abc123")
    path = get_user_config_path(profile_dir)
    assert path == profile_dir / "settings.toml"


def test_get_project_config_name_returns_correct_path() -> None:
    """get_project_config_name should return correct relative path."""
    path = get_project_config_name("mngr")
    assert path == Path(".mngr") / "settings.toml"


def test_get_local_config_name_returns_correct_path() -> None:
    """get_local_config_name should return correct relative path."""
    path = get_local_config_name("mngr")
    assert path == Path(".mngr") / "settings.local.toml"


# =============================================================================
# Tests for read_default_command
# =============================================================================


def test_read_default_command_returns_none_when_no_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """read_default_command should return None when no config files exist."""

    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "nonexistent"))
    monkeypatch.setenv("MNGR_ROOT_NAME", "mngr-test-nocfg")
    assert read_default_command("mngr") is None


def test_read_default_command_reads_from_project_config(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
) -> None:
    """read_default_command should read default_subcommand from project config."""
    (project_config_dir / "settings.toml").write_text('[commands.mngr]\ndefault_subcommand = "list"\n')

    assert read_default_command("mngr") == "list"


def test_read_default_command_local_overrides_project(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
) -> None:
    """read_default_command should let local config override project config."""
    (project_config_dir / "settings.toml").write_text('[commands.mngr]\ndefault_subcommand = "list"\n')
    (project_config_dir / "settings.local.toml").write_text('[commands.mngr]\ndefault_subcommand = "stop"\n')

    assert read_default_command("mngr") == "stop"


def test_read_default_command_empty_string_disables(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
) -> None:
    """read_default_command should return empty string when config disables defaulting."""
    (project_config_dir / "settings.toml").write_text('[commands.mngr]\ndefault_subcommand = ""\n')

    assert read_default_command("mngr") == ""


def test_read_default_command_independent_command_names(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
) -> None:
    """read_default_command should handle multiple command names independently."""
    (project_config_dir / "settings.toml").write_text(
        '[commands.mngr]\ndefault_subcommand = "list"\n\n[commands.snapshot]\ndefault_subcommand = "destroy"\n'
    )

    assert read_default_command("mngr") == "list"
    assert read_default_command("snapshot") == "destroy"
    # Unconfigured groups get None (use compile-time default)
    assert read_default_command("other") is None


# =============================================================================
# Tests for read_disabled_plugins
# =============================================================================


def test_read_disabled_plugins_returns_empty_when_no_config(temp_git_repo_cwd: Path) -> None:
    """read_disabled_plugins should return empty set when no config files exist."""
    assert read_disabled_plugins() == frozenset()


def test_read_disabled_plugins_reads_from_project_config(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
) -> None:
    """read_disabled_plugins should find disabled plugins in project config."""
    (project_config_dir / "settings.toml").write_text("[plugins.modal]\nenabled = false\n")

    assert "modal" in read_disabled_plugins()


def test_read_disabled_plugins_local_overrides_project(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
) -> None:
    """read_disabled_plugins should let local config re-enable a plugin disabled in project config."""
    (project_config_dir / "settings.toml").write_text("[plugins.modal]\nenabled = false\n")
    (project_config_dir / "settings.local.toml").write_text("[plugins.modal]\nenabled = true\n")

    assert "modal" not in read_disabled_plugins()


def test_read_disabled_plugins_multiple_plugins(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
) -> None:
    """read_disabled_plugins should handle multiple disabled plugins."""
    (project_config_dir / "settings.toml").write_text(
        "[plugins.modal]\nenabled = false\n\n[plugins.docker]\nenabled = false\n\n[plugins.local]\nenabled = true\n"
    )

    result = read_disabled_plugins()
    assert "modal" in result
    assert "docker" in result
    assert "local" not in result


# =============================================================================
# Tests for resolve_project_config_dir
# =============================================================================


def test_resolve_project_config_dir_uses_env_var_when_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    temp_git_repo_cwd: Path,
    cg: ConcurrencyGroup,
) -> None:
    """resolve_project_config_dir should use MNGR_PROJECT_DIR when set."""
    custom_dir = tmp_path / "custom_project_config"
    custom_dir.mkdir()
    monkeypatch.setenv("MNGR_PROJECT_DIR", str(custom_dir))

    result = resolve_project_config_dir(None, "mngr", cg)
    assert result == custom_dir


def test_resolve_project_config_dir_falls_back_to_git_root_when_env_var_not_set(
    monkeypatch: pytest.MonkeyPatch,
    temp_git_repo_cwd: Path,
    mngr_test_root_name: str,
    cg: ConcurrencyGroup,
) -> None:
    """resolve_project_config_dir should use <git_root>/.<root_name> when MNGR_PROJECT_DIR is not set."""
    monkeypatch.delenv("MNGR_PROJECT_DIR", raising=False)

    result = resolve_project_config_dir(None, mngr_test_root_name, cg)
    assert result == temp_git_repo_cwd / f".{mngr_test_root_name}"


def test_resolve_project_config_dir_context_dir_overrides_git_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mngr_test_root_name: str,
    cg: ConcurrencyGroup,
) -> None:
    """resolve_project_config_dir should use context_dir when provided and MNGR_PROJECT_DIR is not set."""
    monkeypatch.delenv("MNGR_PROJECT_DIR", raising=False)
    context_dir = tmp_path / "context"
    context_dir.mkdir()

    result = resolve_project_config_dir(context_dir, mngr_test_root_name, cg)
    assert result == context_dir / f".{mngr_test_root_name}"


def test_resolve_project_config_dir_env_var_takes_precedence_over_context_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mngr_test_root_name: str,
    cg: ConcurrencyGroup,
) -> None:
    """resolve_project_config_dir should prefer MNGR_PROJECT_DIR over context_dir."""
    custom_dir = tmp_path / "custom"
    custom_dir.mkdir()
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    monkeypatch.setenv("MNGR_PROJECT_DIR", str(custom_dir))

    result = resolve_project_config_dir(context_dir, mngr_test_root_name, cg)
    assert result == custom_dir


# =============================================================================
# Tests for MNGR_PROJECT_DIR affecting config loading
# =============================================================================


def test_load_project_config_uses_mngr_project_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    """load_project_config should load settings.toml from MNGR_PROJECT_DIR when set."""
    custom_dir = tmp_path / "custom_project"
    custom_dir.mkdir()
    (custom_dir / "settings.toml").write_text('prefix = "custom-"\n')
    monkeypatch.setenv("MNGR_PROJECT_DIR", str(custom_dir))

    result = load_project_config(None, "mngr", cg)
    assert result is not None
    assert result["prefix"] == "custom-"


def test_load_local_config_uses_mngr_project_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    """load_local_config should load settings.local.toml from MNGR_PROJECT_DIR when set."""
    custom_dir = tmp_path / "custom_project"
    custom_dir.mkdir()
    (custom_dir / "settings.local.toml").write_text('prefix = "local-custom-"\n')
    monkeypatch.setenv("MNGR_PROJECT_DIR", str(custom_dir))

    result = load_local_config(None, "mngr", cg)
    assert result is not None
    assert result["prefix"] == "local-custom-"


def test_read_default_command_uses_mngr_project_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """read_default_command should find config from MNGR_PROJECT_DIR."""
    custom_dir = tmp_path / "custom_project"
    custom_dir.mkdir()
    (custom_dir / "settings.toml").write_text('[commands.mngr]\ndefault_subcommand = "create"\n')
    monkeypatch.setenv("MNGR_PROJECT_DIR", str(custom_dir))
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "nonexistent"))

    assert read_default_command("mngr") == "create"


def test_read_disabled_plugins_uses_mngr_project_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """read_disabled_plugins should find config from MNGR_PROJECT_DIR."""
    custom_dir = tmp_path / "custom_project"
    custom_dir.mkdir()
    (custom_dir / "settings.toml").write_text("[plugins.modal]\nenabled = false\n")
    monkeypatch.setenv("MNGR_PROJECT_DIR", str(custom_dir))
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "nonexistent"))

    assert "modal" in read_disabled_plugins()
