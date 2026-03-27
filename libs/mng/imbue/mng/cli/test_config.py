"""Integration tests for the config CLI command."""

import json
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.cli.config import config


def test_config_list_shows_merged_config(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test config list shows the merged configuration."""
    result = cli_runner.invoke(
        config,
        ["list"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "prefix" in result.output


def test_config_list_with_json_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test config list with JSON output format."""
    result = cli_runner.invoke(
        config,
        ["list", "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    output = json.loads(result.output)
    assert "config" in output
    assert "prefix" in output["config"]


def test_config_list_with_scope_shows_file_path(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    """Test config list with scope shows the config file path."""
    # Create a profile directory in the temp_host_dir (where MNG_HOST_DIR points)
    # The setup_test_mng_env autouse fixture sets MNG_HOST_DIR to temp_host_dir
    profile_id = "test-profile-123"
    profile_dir = temp_host_dir / "profiles" / profile_id
    profile_dir.mkdir(parents=True)

    # Create the config.toml that specifies the active profile
    root_config_path = temp_host_dir / "config.toml"
    root_config_path.write_text(f'profile = "{profile_id}"\n')

    # Create the settings.toml in the profile directory
    user_config_path = profile_dir / "settings.toml"
    user_config_path.write_text('prefix = "custom-"\n')

    result = cli_runner.invoke(
        config,
        ["list", "--scope", "user"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "user" in result.output.lower()
    assert "prefix = custom-" in result.output


def test_config_get_retrieves_value(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test config get retrieves a specific configuration value."""
    result = cli_runner.invoke(
        config,
        ["get", "prefix"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    # The prefix should be the test prefix from the fixture
    assert "mng" in result.output.lower()


def test_config_get_with_nested_key(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test config get with a nested key path."""
    result = cli_runner.invoke(
        config,
        ["get", "logging.console_level"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    # Console level should be one of the valid log levels
    assert any(level in result.output.upper() for level in ["INFO", "DEBUG", "BUILD", "WARN", "ERROR", "TRACE"])


def test_config_get_nonexistent_key_fails(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test config get with a nonexistent key returns an error."""
    result = cli_runner.invoke(
        config,
        ["get", "nonexistent.key.path"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_config_get_with_json_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test config get with JSON output format."""
    result = cli_runner.invoke(
        config,
        ["get", "prefix", "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    output = json.loads(result.output)
    assert "key" in output
    assert output["key"] == "prefix"
    assert "value" in output


def test_config_set_creates_config_file(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mng_test_root_name: str,
) -> None:
    """Test config set creates a new config file if it doesn't exist."""
    monkeypatch.chdir(temp_git_repo)

    result = cli_runner.invoke(
        config,
        ["set", "prefix", "my-prefix-", "--scope", "project"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Set prefix" in result.output

    # Verify the file was created (using the test root name)
    config_path = temp_git_repo / f".{mng_test_root_name}" / "settings.toml"
    assert config_path.exists()
    content = config_path.read_text()
    assert 'prefix = "my-prefix-"' in content


def test_config_set_nested_key(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mng_test_root_name: str,
) -> None:
    """Test config set with a nested key path."""
    monkeypatch.chdir(temp_git_repo)

    result = cli_runner.invoke(
        config,
        ["set", "commands.create.connect", "false", "--scope", "project"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0

    # Verify the nested structure was created (using the test root name)
    config_path = temp_git_repo / f".{mng_test_root_name}" / "settings.toml"
    content = config_path.read_text()
    assert "[commands.create]" in content
    assert "connect = false" in content


def test_config_set_parses_boolean_values(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mng_test_root_name: str,
) -> None:
    """Test config set correctly parses boolean values."""
    monkeypatch.chdir(temp_git_repo)

    # Set true value
    result = cli_runner.invoke(
        config,
        ["set", "is_nested_tmux_allowed", "true", "--scope", "project"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0

    config_path = temp_git_repo / f".{mng_test_root_name}" / "settings.toml"
    content = config_path.read_text()
    assert "is_nested_tmux_allowed = true" in content


def test_config_set_parses_integer_values(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mng_test_root_name: str,
) -> None:
    """Test config set correctly parses integer values."""
    monkeypatch.chdir(temp_git_repo)

    result = cli_runner.invoke(
        config,
        ["set", "default_destroyed_host_persisted_seconds", "42", "--scope", "project"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0

    config_path = temp_git_repo / f".{mng_test_root_name}" / "settings.toml"
    content = config_path.read_text()
    assert "default_destroyed_host_persisted_seconds = 42" in content


def test_config_set_rejects_unknown_top_level_field(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mng_test_root_name: str,
) -> None:
    """Test config set rejects unknown top-level configuration fields."""
    monkeypatch.chdir(temp_git_repo)

    result = cli_runner.invoke(
        config,
        ["set", "provider.default", "docker", "--scope", "project"],
        obj=plugin_manager,
    )
    assert result.exit_code == 1
    assert "Invalid configuration" in result.output

    # Verify the file was NOT created/modified
    config_path = temp_git_repo / f".{mng_test_root_name}" / "settings.toml"
    if config_path.exists():
        content = config_path.read_text()
        assert "provider" not in content


def test_config_unset_removes_value(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mng_test_root_name: str,
) -> None:
    """Test config unset removes an existing value."""
    monkeypatch.chdir(temp_git_repo)

    # First set a value (using the test root name)
    config_dir = temp_git_repo / f".{mng_test_root_name}"
    config_dir.mkdir()
    config_path = config_dir / "settings.toml"
    config_path.write_text('prefix = "test-"\ndefault_host_dir = "/tmp/keep"\n')

    # Then unset it
    result = cli_runner.invoke(
        config,
        ["unset", "prefix", "--scope", "project"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed prefix" in result.output

    # Verify the value was removed but other values remain
    content = config_path.read_text()
    assert "prefix" not in content
    assert "default_host_dir" in content


def test_config_unset_nonexistent_key_fails(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mng_test_root_name: str,
) -> None:
    """Test config unset with nonexistent key returns an error."""
    monkeypatch.chdir(temp_git_repo)

    # Create an empty config (using the test root name)
    config_dir = temp_git_repo / f".{mng_test_root_name}"
    config_dir.mkdir()
    config_path = config_dir / "settings.toml"
    config_path.write_text("")

    result = cli_runner.invoke(
        config,
        ["unset", "nonexistent", "--scope", "project"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_config_path_shows_all_paths(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test config path shows all config file paths."""
    result = cli_runner.invoke(
        config,
        ["path"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "user" in result.output.lower()


def test_config_path_with_scope_shows_single_path(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test config path with scope shows a single path."""
    result = cli_runner.invoke(
        config,
        ["path", "--scope", "user"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "settings.toml" in result.output


def test_config_path_with_json_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test config path with JSON output format."""
    result = cli_runner.invoke(
        config,
        ["path", "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    output = json.loads(result.output)
    assert "paths" in output
    assert len(output["paths"]) > 0
