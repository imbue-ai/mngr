import json
from pathlib import Path
from typing import Any

import pluggy
import pytest
from loguru import logger

from imbue.mng.cli.config import ConfigScope
from imbue.mng.cli.output_helpers import AbortError
from imbue.mng.cli.plugin import PluginCliOptions
from imbue.mng.cli.plugin import PluginInfo
from imbue.mng.cli.plugin import _GitSource
from imbue.mng.cli.plugin import _PathSource
from imbue.mng.cli.plugin import _PypiSource
from imbue.mng.cli.plugin import _emit_plugin_add_result
from imbue.mng.cli.plugin import _emit_plugin_list
from imbue.mng.cli.plugin import _emit_plugin_remove_result
from imbue.mng.cli.plugin import _emit_plugin_toggle_result
from imbue.mng.cli.plugin import _gather_plugin_info
from imbue.mng.cli.plugin import _get_field_value
from imbue.mng.cli.plugin import _get_installed_package_names
from imbue.mng.cli.plugin import _is_plugin_enabled
from imbue.mng.cli.plugin import _parse_add_source
from imbue.mng.cli.plugin import _parse_fields
from imbue.mng.cli.plugin import _parse_pypi_package_name
from imbue.mng.cli.plugin import _parse_remove_source
from imbue.mng.cli.plugin import _read_package_name_from_pyproject
from imbue.mng.cli.plugin import _validate_plugin_name_is_known
from imbue.mng.config.data_types import MngConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.config.data_types import PluginConfig
from imbue.mng.errors import PluginSpecifierError
from imbue.mng.plugins import hookspecs
from imbue.mng.primitives import OutputFormat
from imbue.mng.primitives import PluginName

# =============================================================================
# Tests for PluginInfo model
# =============================================================================


def test_plugin_info_model_creates_with_all_fields() -> None:
    """PluginInfo should create with all fields provided."""
    info = PluginInfo(
        name="my-plugin",
        version="1.2.3",
        description="A test plugin",
        is_enabled=True,
    )
    assert info.name == "my-plugin"
    assert info.version == "1.2.3"
    assert info.description == "A test plugin"
    assert info.is_enabled is True


def test_plugin_info_model_defaults() -> None:
    """PluginInfo should use None defaults for optional fields."""
    info = PluginInfo(name="minimal", is_enabled=False)
    assert info.name == "minimal"
    assert info.version is None
    assert info.description is None
    assert info.is_enabled is False


# =============================================================================
# Tests for _is_plugin_enabled
# =============================================================================


def test_is_plugin_enabled_returns_true_by_default() -> None:
    """_is_plugin_enabled should return True for unknown plugins."""
    config = MngConfig()
    assert _is_plugin_enabled("some-plugin", config) is True


def test_is_plugin_enabled_returns_false_for_disabled_plugins_set() -> None:
    """_is_plugin_enabled should return False for plugins in disabled_plugins."""
    config = MngConfig(disabled_plugins=frozenset({"disabled-one"}))
    assert _is_plugin_enabled("disabled-one", config) is False
    assert _is_plugin_enabled("other-plugin", config) is True


def test_is_plugin_enabled_returns_false_for_config_enabled_false() -> None:
    """_is_plugin_enabled should return False for plugins with enabled=False in plugins dict."""
    config = MngConfig(
        plugins={
            PluginName("off-plugin"): PluginConfig(enabled=False),
            PluginName("on-plugin"): PluginConfig(enabled=True),
        }
    )
    assert _is_plugin_enabled("off-plugin", config) is False
    assert _is_plugin_enabled("on-plugin", config) is True


# =============================================================================
# Tests for _get_field_value
# =============================================================================


def test_get_field_value_name() -> None:
    """_get_field_value should return name."""
    info = PluginInfo(name="test", is_enabled=True)
    assert _get_field_value(info, "name") == "test"


def test_get_field_value_version_present() -> None:
    """_get_field_value should return version when present."""
    info = PluginInfo(name="test", version="1.0", is_enabled=True)
    assert _get_field_value(info, "version") == "1.0"


def test_get_field_value_version_none() -> None:
    """_get_field_value should return '-' when version is None."""
    info = PluginInfo(name="test", is_enabled=True)
    assert _get_field_value(info, "version") == "-"


def test_get_field_value_description_present() -> None:
    """_get_field_value should return description when present."""
    info = PluginInfo(name="test", description="A plugin", is_enabled=True)
    assert _get_field_value(info, "description") == "A plugin"


def test_get_field_value_description_none() -> None:
    """_get_field_value should return '-' when description is None."""
    info = PluginInfo(name="test", is_enabled=True)
    assert _get_field_value(info, "description") == "-"


def test_get_field_value_enabled_true() -> None:
    """_get_field_value should return 'true' for enabled plugins."""
    info = PluginInfo(name="test", is_enabled=True)
    assert _get_field_value(info, "enabled") == "true"


def test_get_field_value_enabled_false() -> None:
    """_get_field_value should return 'false' for disabled plugins."""
    info = PluginInfo(name="test", is_enabled=False)
    assert _get_field_value(info, "enabled") == "false"


def test_get_field_value_unknown_field() -> None:
    """_get_field_value should return '-' for unknown fields."""
    info = PluginInfo(name="test", is_enabled=True)
    assert _get_field_value(info, "nonexistent") == "-"


# =============================================================================
# Tests for _parse_fields
# =============================================================================


def test_parse_fields_none_returns_defaults() -> None:
    """_parse_fields should return default fields when given None."""
    fields = _parse_fields(None)
    assert fields == ("name", "version", "description", "enabled")


def test_parse_fields_custom() -> None:
    """_parse_fields should parse comma-separated field names."""
    fields = _parse_fields("name,enabled")
    assert fields == ("name", "enabled")


def test_parse_fields_with_spaces() -> None:
    """_parse_fields should strip whitespace from field names."""
    fields = _parse_fields(" name , version ")
    assert fields == ("name", "version")


# =============================================================================
# Tests for _emit_plugin_list
# =============================================================================


def _make_test_plugins() -> list[PluginInfo]:
    """Create a list of test plugins."""
    return [
        PluginInfo(name="alpha", version="1.0", description="First", is_enabled=True),
        PluginInfo(name="beta", version="2.0", description="Second", is_enabled=False),
    ]


def test_emit_plugin_list_human_format_renders_table(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_plugin_list with HUMAN format should render a table via logger."""
    plugins = _make_test_plugins()
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    # This outputs via logger, so we just verify no exception
    _emit_plugin_list(plugins, output_opts, ("name", "version", "description", "enabled"))


def test_emit_plugin_list_human_format_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_plugin_list with HUMAN format should handle empty list."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_plugin_list([], output_opts, ("name", "version", "description", "enabled"))


def test_emit_plugin_list_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_plugin_list with JSON format should output valid JSON."""
    plugins = _make_test_plugins()
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _emit_plugin_list(plugins, output_opts, ("name", "version", "description", "enabled"))

    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert "plugins" in data
    assert len(data["plugins"]) == 2
    assert data["plugins"][0]["name"] == "alpha"
    assert data["plugins"][0]["version"] == "1.0"
    assert data["plugins"][1]["name"] == "beta"
    assert data["plugins"][1]["enabled"] == "false"


def test_emit_plugin_list_jsonl_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_plugin_list with JSONL format should output one line per plugin."""
    plugins = _make_test_plugins()
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    _emit_plugin_list(plugins, output_opts, ("name", "enabled"))

    captured = capsys.readouterr()
    lines = captured.out.strip().split("\n")
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert first["name"] == "alpha"
    assert first["enabled"] == "true"

    second = json.loads(lines[1])
    assert second["name"] == "beta"
    assert second["enabled"] == "false"


def test_emit_plugin_list_with_field_selection(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_plugin_list should respect field selection."""
    plugins = _make_test_plugins()
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _emit_plugin_list(plugins, output_opts, ("name", "enabled"))

    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    # Only selected fields should appear
    assert set(data["plugins"][0].keys()) == {"name", "enabled"}


# =============================================================================
# Tests for _gather_plugin_info
# =============================================================================


def test_gather_plugin_info_returns_sorted_list() -> None:
    """_gather_plugin_info should return plugins sorted by name."""
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)

    # Register some test plugins with explicit names
    class PluginZ:
        pass

    class PluginA:
        pass

    pm.register(PluginZ(), name="zebra-plugin")
    pm.register(PluginA(), name="alpha-plugin")

    config = MngConfig()
    mng_ctx = MngContext(
        config=config,
        pm=pm,
        profile_dir=_fake_profile_dir(),
    )

    plugins = _gather_plugin_info(mng_ctx)
    names = [p.name for p in plugins]
    assert names == sorted(names)
    assert "alpha-plugin" in names
    assert "zebra-plugin" in names


def test_gather_plugin_info_reflects_disabled_status() -> None:
    """_gather_plugin_info should mark disabled plugins correctly."""
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)

    class MyPlugin:
        pass

    pm.register(MyPlugin(), name="my-plugin")

    config = MngConfig(disabled_plugins=frozenset({"my-plugin"}))
    mng_ctx = MngContext(
        config=config,
        pm=pm,
        profile_dir=_fake_profile_dir(),
    )

    plugins = _gather_plugin_info(mng_ctx)
    my_plugin = next(p for p in plugins if p.name == "my-plugin")
    assert my_plugin.is_enabled is False


def test_gather_plugin_info_skips_internal_plugins() -> None:
    """_gather_plugin_info should skip plugins with names starting with underscore."""
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)

    class InternalPlugin:
        pass

    class PublicPlugin:
        pass

    pm.register(InternalPlugin(), name="_internal")
    pm.register(PublicPlugin(), name="public-plugin")

    config = MngConfig()
    mng_ctx = MngContext(
        config=config,
        pm=pm,
        profile_dir=_fake_profile_dir(),
    )

    plugins = _gather_plugin_info(mng_ctx)
    names = [p.name for p in plugins]
    assert "_internal" not in names
    assert "public-plugin" in names


def _fake_profile_dir() -> Path:
    """Return a fake profile directory path for testing."""
    return Path("/tmp/fake-mng-profile")


# =============================================================================
# Tests for _validate_plugin_name_is_known
# =============================================================================


def test_validate_plugin_name_is_known_no_warning_for_known() -> None:
    """_validate_plugin_name_is_known should not warn for a known plugin."""
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)

    class MyPlugin:
        pass

    pm.register(MyPlugin(), name="known-plugin")

    mng_ctx = MngContext(
        config=MngConfig(),
        pm=pm,
        profile_dir=_fake_profile_dir(),
    )

    warnings: list[str] = []
    sink_id = logger.add(lambda msg: warnings.append(str(msg)), level="WARNING")
    try:
        _validate_plugin_name_is_known("known-plugin", mng_ctx)
    finally:
        logger.remove(sink_id)

    assert not any("not currently registered" in w for w in warnings)


def test_validate_plugin_name_is_known_warns_for_unknown() -> None:
    """_validate_plugin_name_is_known should warn for an unknown plugin."""
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)

    mng_ctx = MngContext(
        config=MngConfig(),
        pm=pm,
        profile_dir=_fake_profile_dir(),
    )

    warnings: list[str] = []
    sink_id = logger.add(lambda msg: warnings.append(str(msg)), level="WARNING")
    try:
        _validate_plugin_name_is_known("nonexistent-plugin", mng_ctx)
    finally:
        logger.remove(sink_id)

    assert any("not currently registered" in w for w in warnings)


# =============================================================================
# Tests for _emit_plugin_toggle_result
# =============================================================================


def test_emit_plugin_toggle_result_json_enable(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_plugin_toggle_result should output valid JSON for enable."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    config_path = Path("/tmp/test/.mng/settings.toml")

    _emit_plugin_toggle_result("modal", True, ConfigScope.PROJECT, config_path, output_opts)

    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["plugin"] == "modal"
    assert data["enabled"] is True
    assert data["scope"] == "project"
    assert data["path"] == str(config_path)


def test_emit_plugin_toggle_result_json_disable(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_plugin_toggle_result should output valid JSON for disable."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    config_path = Path("/tmp/test/.mng/settings.toml")

    _emit_plugin_toggle_result("modal", False, ConfigScope.PROJECT, config_path, output_opts)

    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["plugin"] == "modal"
    assert data["enabled"] is False


def test_emit_plugin_toggle_result_jsonl_has_event_type(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_plugin_toggle_result with JSONL should include event type."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    config_path = Path("/tmp/test/.mng/settings.toml")

    _emit_plugin_toggle_result("modal", True, ConfigScope.PROJECT, config_path, output_opts)

    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "plugin_toggled"
    assert data["plugin"] == "modal"
    assert data["enabled"] is True


# =============================================================================
# Tests for _parse_pypi_package_name
# =============================================================================


def test_parse_pypi_package_name_valid_name() -> None:
    """_parse_pypi_package_name should return the package name for a valid specifier."""
    assert _parse_pypi_package_name("mng-opencode") == "mng-opencode"


def test_parse_pypi_package_name_name_with_version() -> None:
    """_parse_pypi_package_name should return the package name for specifiers with versions."""
    assert _parse_pypi_package_name("mng-opencode>=1.0") == "mng-opencode"


def test_parse_pypi_package_name_invalid_format() -> None:
    """_parse_pypi_package_name should return None for invalid specifiers."""
    assert _parse_pypi_package_name("not a valid!!spec$$") is None


# =============================================================================
# Tests for _get_installed_package_names
# =============================================================================


def test_get_installed_package_names_returns_package_names() -> None:
    """_get_installed_package_names should return a set of installed package names."""

    class FakeConcurrencyGroup:
        def run_process_to_completion(self, command: tuple[str, ...]) -> Any:
            class Result:
                stdout = json.dumps(
                    [
                        {"name": "mng", "version": "1.0.0"},
                        {"name": "mng-opencode", "version": "0.1.0"},
                        {"name": "pluggy", "version": "1.5.0"},
                    ]
                )

            return Result()

    names = _get_installed_package_names(FakeConcurrencyGroup())
    assert names == {"mng", "mng-opencode", "pluggy"}


def test_get_installed_package_names_empty_list() -> None:
    """_get_installed_package_names should return an empty set when no packages are installed."""

    class FakeConcurrencyGroup:
        def run_process_to_completion(self, command: tuple[str, ...]) -> Any:
            class Result:
                stdout = "[]"

            return Result()

    names = _get_installed_package_names(FakeConcurrencyGroup())
    assert names == set()


# =============================================================================
# Tests for _read_package_name_from_pyproject
# =============================================================================


def test_read_package_name_from_pyproject_valid(tmp_path: Path) -> None:
    """_read_package_name_from_pyproject should read name from pyproject.toml."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "my-test-plugin"\n')

    assert _read_package_name_from_pyproject(str(tmp_path)) == "my-test-plugin"


def test_read_package_name_from_pyproject_missing_file(tmp_path: Path) -> None:
    """_read_package_name_from_pyproject should raise PluginSpecifierError if no pyproject.toml found."""
    with pytest.raises(PluginSpecifierError, match="No pyproject.toml found"):
        _read_package_name_from_pyproject(str(tmp_path))


def test_read_package_name_from_pyproject_missing_name(tmp_path: Path) -> None:
    """_read_package_name_from_pyproject should raise PluginSpecifierError if project.name is absent."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.1.0"\n')

    with pytest.raises(PluginSpecifierError, match="does not have a project.name field"):
        _read_package_name_from_pyproject(str(tmp_path))


# =============================================================================
# Tests for _emit_plugin_add_result
# =============================================================================


def test_emit_plugin_add_result_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_plugin_add_result with JSON format should output valid JSON."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _emit_plugin_add_result("mng-opencode", "mng-opencode", True, output_opts)

    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["specifier"] == "mng-opencode"
    assert data["package"] == "mng-opencode"
    assert data["has_entry_points"] is True


def test_emit_plugin_add_result_jsonl_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_plugin_add_result with JSONL format should include event type."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    _emit_plugin_add_result("./my-plugin", "my-plugin", False, output_opts)

    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "plugin_added"
    assert data["specifier"] == "./my-plugin"
    assert data["package"] == "my-plugin"
    assert data["has_entry_points"] is False


# =============================================================================
# Tests for _emit_plugin_remove_result
# =============================================================================


def test_emit_plugin_remove_result_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_plugin_remove_result with JSON format should output valid JSON."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _emit_plugin_remove_result("mng-opencode", output_opts)

    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["package"] == "mng-opencode"


def test_emit_plugin_remove_result_jsonl_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_plugin_remove_result with JSONL format should include event type."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    _emit_plugin_remove_result("mng-opencode", output_opts)

    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "plugin_removed"
    assert data["package"] == "mng-opencode"


# =============================================================================
# Helpers for _parse_add_source / _parse_remove_source tests
# =============================================================================


def _make_plugin_cli_options(
    name: str | None = None,
    path: str | None = None,
    git: str | None = None,
) -> PluginCliOptions:
    """Create a PluginCliOptions with the given source fields and minimal defaults."""
    return PluginCliOptions(
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
        name=name,
        path=path,
        git=git,
    )


# =============================================================================
# Tests for _parse_add_source
# =============================================================================


def test_parse_add_source_no_source_raises_abort() -> None:
    """_parse_add_source should raise AbortError when no source is provided."""
    opts = _make_plugin_cli_options()
    with pytest.raises(AbortError, match="Provide exactly one of NAME, --path, or --git"):
        _parse_add_source(opts)


def test_parse_add_source_multiple_sources_raises_abort() -> None:
    """_parse_add_source should raise AbortError when multiple sources are provided."""
    opts = _make_plugin_cli_options(name="mng-opencode", path="./my-plugin")
    with pytest.raises(AbortError, match="mutually exclusive"):
        _parse_add_source(opts)


def test_parse_add_source_name_and_git_raises_abort() -> None:
    """_parse_add_source should raise AbortError when name and git are both provided."""
    opts = _make_plugin_cli_options(name="mng-opencode", git="https://github.com/user/repo.git")
    with pytest.raises(AbortError, match="mutually exclusive"):
        _parse_add_source(opts)


def test_parse_add_source_valid_pypi_name() -> None:
    """_parse_add_source should return _PypiSource for a valid PyPI name."""
    opts = _make_plugin_cli_options(name="mng-opencode")
    source = _parse_add_source(opts)
    assert isinstance(source, _PypiSource)
    assert source.name == "mng-opencode"


def test_parse_add_source_valid_pypi_name_with_version() -> None:
    """_parse_add_source should return _PypiSource for a name with version constraint."""
    opts = _make_plugin_cli_options(name="mng-opencode>=1.0")
    source = _parse_add_source(opts)
    assert isinstance(source, _PypiSource)
    assert source.name == "mng-opencode>=1.0"


def test_parse_add_source_valid_path() -> None:
    """_parse_add_source should return _PathSource for a path."""
    opts = _make_plugin_cli_options(path="./my-plugin")
    source = _parse_add_source(opts)
    assert isinstance(source, _PathSource)
    assert source.path == "./my-plugin"


def test_parse_add_source_valid_git_url() -> None:
    """_parse_add_source should return _GitSource for a git URL."""
    opts = _make_plugin_cli_options(git="https://github.com/user/repo.git")
    source = _parse_add_source(opts)
    assert isinstance(source, _GitSource)
    assert source.url == "https://github.com/user/repo.git"


def test_parse_add_source_invalid_name_raises_abort() -> None:
    """_parse_add_source should raise AbortError for an invalid package name."""
    opts = _make_plugin_cli_options(name="not a valid!!spec$$")
    with pytest.raises(AbortError, match="Invalid package name"):
        _parse_add_source(opts)


# =============================================================================
# Tests for _parse_remove_source
# =============================================================================


def test_parse_remove_source_no_source_raises_abort() -> None:
    """_parse_remove_source should raise AbortError when no source is provided."""
    opts = _make_plugin_cli_options()
    with pytest.raises(AbortError, match="Provide exactly one of NAME or --path"):
        _parse_remove_source(opts)


def test_parse_remove_source_multiple_sources_raises_abort() -> None:
    """_parse_remove_source should raise AbortError when both name and path are provided."""
    opts = _make_plugin_cli_options(name="mng-opencode", path="./my-plugin")
    with pytest.raises(AbortError, match="mutually exclusive"):
        _parse_remove_source(opts)


def test_parse_remove_source_valid_pypi_name() -> None:
    """_parse_remove_source should return _PypiSource for a valid PyPI name."""
    opts = _make_plugin_cli_options(name="mng-opencode")
    source = _parse_remove_source(opts)
    assert isinstance(source, _PypiSource)
    assert source.name == "mng-opencode"


def test_parse_remove_source_valid_path() -> None:
    """_parse_remove_source should return _PathSource for a path."""
    opts = _make_plugin_cli_options(path="./my-plugin")
    source = _parse_remove_source(opts)
    assert isinstance(source, _PathSource)
    assert source.path == "./my-plugin"


def test_parse_remove_source_invalid_name_raises_abort() -> None:
    """_parse_remove_source should raise AbortError for an invalid package name."""
    opts = _make_plugin_cli_options(name="not a valid!!spec$$")
    with pytest.raises(AbortError, match="Invalid package name"):
        _parse_remove_source(opts)
