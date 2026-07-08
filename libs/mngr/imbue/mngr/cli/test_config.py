"""Integration tests for the config CLI command."""

import json
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.config import config


def test_config_list_shows_merged_config(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test config list --all shows the merged configuration including defaults.

    ``mngr config list`` without ``--all`` only lists keys that have been
    explicitly written to a TOML scope; in this clean test environment that's
    empty. ``--all`` includes default-valued fields so ``prefix`` (which has a
    default of ``mngr-``) appears.
    """
    result = cli_runner.invoke(
        config,
        ["list", "--all"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "prefix" in result.output


def test_config_list_with_json_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test config list --all with JSON output format."""
    result = cli_runner.invoke(
        config,
        ["list", "--all", "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    output = json.loads(result.output)
    assert "config" in output
    assert "prefix" in output["config"]


def test_config_list_without_all_omits_default_only_keys(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Without ``--all``, keys that only have default values are omitted.

    Regression test for the previous implementation where ``--all`` was a
    no-op: the default view dumped every field regardless of whether the user
    had ever written it to a TOML file.
    """
    result = cli_runner.invoke(
        config,
        ["list", "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    output = json.loads(result.output)
    assert "config" in output
    # In the clean test env no TOML scopes are written; ``prefix`` (a default-
    # only field here) must not appear in the explicit-keys-only listing.
    assert "prefix" not in output["config"]


def test_config_list_with_scope_shows_file_path(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    """Test config list with scope shows the config file path."""
    # Create a profile directory in the temp_host_dir (where MNGR_HOST_DIR points)
    # The setup_test_mngr_env autouse fixture sets MNGR_HOST_DIR to temp_host_dir
    profile_id = "test-profile-123"
    profile_dir = temp_host_dir / "profiles" / profile_id
    profile_dir.mkdir(parents=True)

    # Create the config.toml that specifies the active profile
    root_config_path = temp_host_dir / "config.toml"
    root_config_path.write_text(f'profile = "{profile_id}"\n')

    # Create the settings.toml in the profile directory
    user_config_path = profile_dir / "settings.toml"
    user_config_path.write_text('prefix = "custom-"\nis_allowed_in_pytest = true\n')

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
    assert "mngr" in result.output.lower()


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
    mngr_test_root_name: str,
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
    config_path = temp_git_repo / f".{mngr_test_root_name}" / "settings.toml"
    assert config_path.exists()
    content = config_path.read_text()
    assert 'prefix = "my-prefix-"' in content


def test_config_set_nested_key(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mngr_test_root_name: str,
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
    config_path = temp_git_repo / f".{mngr_test_root_name}" / "settings.toml"
    content = config_path.read_text()
    assert "[commands.create]" in content
    assert "connect = false" in content


def test_config_set_parses_boolean_values(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mngr_test_root_name: str,
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

    config_path = temp_git_repo / f".{mngr_test_root_name}" / "settings.toml"
    content = config_path.read_text()
    assert "is_nested_tmux_allowed = true" in content


def test_config_set_parses_integer_values(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mngr_test_root_name: str,
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

    config_path = temp_git_repo / f".{mngr_test_root_name}" / "settings.toml"
    content = config_path.read_text()
    assert "default_destroyed_host_persisted_seconds = 42" in content


def test_config_set_rejects_unknown_top_level_field(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mngr_test_root_name: str,
) -> None:
    """Test config set rejects unknown top-level configuration fields."""
    monkeypatch.chdir(temp_git_repo)

    result = cli_runner.invoke(
        config,
        ["set", "provider.default", "docker", "--scope", "project"],
        obj=plugin_manager,
    )
    assert result.exit_code == 1
    # The ConfigParseError is a MngrError, rendered cleanly by the central CLI handler.
    assert "Unknown configuration fields" in result.output
    assert "provider" in result.output

    # Verify the file was NOT created/modified
    config_path = temp_git_repo / f".{mngr_test_root_name}" / "settings.toml"
    if config_path.exists():
        content = config_path.read_text()
        assert "provider" not in content


def test_config_unset_removes_value(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mngr_test_root_name: str,
) -> None:
    """Test config unset removes an existing value."""
    monkeypatch.chdir(temp_git_repo)

    # First set a value (using the test root name)
    config_dir = temp_git_repo / f".{mngr_test_root_name}"
    config_dir.mkdir()
    config_path = config_dir / "settings.toml"
    config_path.write_text('is_allowed_in_pytest = true\nprefix = "test-"\ndefault_host_dir = "/tmp/keep"\n')

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
    mngr_test_root_name: str,
) -> None:
    """Test config unset with nonexistent key returns an error."""
    monkeypatch.chdir(temp_git_repo)

    # Create a config with only the pytest opt-in (using the test root name)
    config_dir = temp_git_repo / f".{mngr_test_root_name}"
    config_dir.mkdir()
    config_path = config_dir / "settings.toml"
    config_path.write_text("is_allowed_in_pytest = true\n")

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


def test_config_list_schema_preserves_generic_type_parameters(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """`mngr config list --schema` should render parameterised generics with their args.

    Regression test: ``render_annotation`` (in utils/model_schema) previously
    returned just ``"list"`` for ``list[str]`` (via ``__name__``), losing the
    type parameter -- which
    defeats the schema's purpose of telling users what values a setting takes.
    The renderer must emit ``"list[str]"`` (or equivalent) for parameterised
    annotations.
    """
    result = cli_runner.invoke(
        config,
        ["list", "--schema", "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    output = json.loads(result.output)
    rows_by_key = {row["key"]: row for row in output["schema"]}
    # ``unset_vars: list[str]`` should keep the [str] in the rendered type.
    unset_vars_type = rows_by_key["unset_vars"]["type"]
    assert "list" in unset_vars_type and "str" in unset_vars_type, (
        f"expected unset_vars type to mention both 'list' and 'str', got: {unset_vars_type!r}"
    )


def test_config_list_schema_lists_top_level_fields(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """`mngr config list --schema` should enumerate the well-known top-level MngrConfig fields.

    Spot-checks the discovery surface so the schema view stays useful for
    surfacing settable keys via the documented entry points (MNGR__*,
    --setting, mngr config set).
    """
    result = cli_runner.invoke(
        config,
        ["list", "--schema", "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    keys = {row["key"] for row in output["schema"]}
    assert {"prefix", "default_host_dir", "unset_vars", "headless"} <= keys


def test_config_list_schema_rejects_scope_combination(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """``--schema`` is global; pairing it with ``--scope`` is a UsageError.

    The schema is derived from ``MngrConfig.model_fields``, not from any
    scope-specific TOML file, so combining the two flags has no meaningful
    interpretation. Reject it loudly so the user picks one.
    """
    result = cli_runner.invoke(
        config,
        ["list", "--schema", "--scope", "user"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    assert "--schema and --scope cannot be combined" in result.output


def test_config_extend_writes_extend_suffixed_key(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mngr_test_root_name: str,
) -> None:
    """`mngr config extend` should write a ``field__extend`` entry, not the bare field.

    Locks down the on-disk TOML shape that downstream tooling and the
    resolver rely on -- a regression here would silently change semantics
    from "extend the base value" to "replace it".
    """
    monkeypatch.chdir(temp_git_repo)
    result = cli_runner.invoke(
        config,
        ["extend", "unset_vars", '["FROM_EXTEND"]', "--scope", "project"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    config_path = temp_git_repo / f".{mngr_test_root_name}" / "settings.toml"
    content = config_path.read_text()
    assert 'unset_vars__extend = ["FROM_EXTEND"]' in content, content


def test_config_extend_settings_overrides_writes_mngr_merge(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mngr_test_root_name: str,
) -> None:
    """`mngr config extend` on a ``settings_overrides`` path writes the bare value plus a
    `__mngr_merge` ``extend`` directive -- not a ``__extend`` suffix, which would leak into
    the external CLI's settings.json as a junk key."""
    monkeypatch.chdir(temp_git_repo)
    result = cli_runner.invoke(
        config,
        [
            "extend",
            "agent_types.claude.settings_overrides.permissions.allow",
            '["Bash(npm *)"]',
            "--scope",
            "project",
        ],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    content = (temp_git_repo / f".{mngr_test_root_name}" / "settings.toml").read_text()
    assert "allow__extend" not in content, content
    assert '"permissions.allow" = "extend"' in content, content
    assert 'allow = ["Bash(npm *)"]' in content, content


def test_config_set_with_extend_suffix_routes_to_extend(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mngr_test_root_name: str,
) -> None:
    """`mngr config set KEY__extend VALUE` should produce the same on-disk
    shape as `mngr config extend KEY VALUE`.

    This is the documented alias spelling for users who don't know about the
    separate `extend` verb; the routing in _config_set_impl must end up at
    the same TOML write.
    """
    monkeypatch.chdir(temp_git_repo)
    result = cli_runner.invoke(
        config,
        ["set", "unset_vars__extend", '["FROM_SET_EXTEND"]', "--scope", "project"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    config_path = temp_git_repo / f".{mngr_test_root_name}" / "settings.toml"
    content = config_path.read_text()
    assert 'unset_vars__extend = ["FROM_SET_EXTEND"]' in content, content


def test_config_assign_writes_assign_suffixed_key(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mngr_test_root_name: str,
) -> None:
    """`mngr config assign` writes a ``field__assign`` entry (replace without the narrowing guard)."""
    monkeypatch.chdir(temp_git_repo)
    result = cli_runner.invoke(
        config,
        ["assign", "unset_vars", '["ONLY"]', "--scope", "project"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    content = (temp_git_repo / f".{mngr_test_root_name}" / "settings.toml").read_text()
    assert 'unset_vars__assign = ["ONLY"]' in content, content


def test_config_set_with_assign_suffix_routes_to_assign(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mngr_test_root_name: str,
) -> None:
    """`mngr config set KEY__assign VALUE` routes to the same write as `mngr config assign`."""
    monkeypatch.chdir(temp_git_repo)
    result = cli_runner.invoke(
        config,
        ["set", "unset_vars__assign", '["ONLY"]', "--scope", "project"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    content = (temp_git_repo / f".{mngr_test_root_name}" / "settings.toml").read_text()
    assert 'unset_vars__assign = ["ONLY"]' in content, content


def test_config_assign_settings_overrides_writes_mngr_merge(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mngr_test_root_name: str,
) -> None:
    """`mngr config assign` on a settings_overrides path writes the bare value plus a
    `__mngr_merge` ``assign`` directive, not an ``__assign`` suffix key."""
    monkeypatch.chdir(temp_git_repo)
    result = cli_runner.invoke(
        config,
        ["assign", "agent_types.claude.settings_overrides.permissions.allow", '["Bash(npm *)"]', "--scope", "project"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    content = (temp_git_repo / f".{mngr_test_root_name}" / "settings.toml").read_text()
    assert "allow__assign" not in content, content
    assert '"permissions.allow" = "assign"' in content, content


def test_config_extend_accumulates_second_mngr_merge_directive(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mngr_test_root_name: str,
) -> None:
    """A second `config extend`/`assign` on a different settings_overrides leaf preserves the
    first directive in the shared `__mngr_merge` map rather than clobbering it."""
    monkeypatch.chdir(temp_git_repo)
    # Pre-seed the project config with the pytest opt-in so the second invocation (which
    # reloads the file the first one wrote) passes the test-config guard.
    config_path = temp_git_repo / f".{mngr_test_root_name}" / "settings.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("is_allowed_in_pytest = true\n")
    base = "agent_types.claude.settings_overrides.permissions"
    for verb, leaf, value in (("extend", "allow", '["A"]'), ("assign", "deny", '["D"]')):
        result = cli_runner.invoke(
            config, [verb, f"{base}.{leaf}", value, "--scope", "project"], obj=plugin_manager, catch_exceptions=False
        )
        assert result.exit_code == 0, result.output
    content = config_path.read_text()
    assert '"permissions.allow" = "extend"' in content, content
    assert '"permissions.deny" = "assign"' in content, content
