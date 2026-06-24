import json
from pathlib import Path
from typing import Any

import pluggy
import pytest
from loguru import logger

from imbue.mngr.agents.agent_registry import _register_agent
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.cli.plugin import PluginCliOptions
from imbue.mngr.cli.plugin import PluginInfo
from imbue.mngr.cli.plugin import _GitSource
from imbue.mngr.cli.plugin import _PathSource
from imbue.mngr.cli.plugin import _PypiSource
from imbue.mngr.cli.plugin import _emit_plugin_add_result
from imbue.mngr.cli.plugin import _emit_plugin_list
from imbue.mngr.cli.plugin import _emit_plugin_remove_result
from imbue.mngr.cli.plugin import _emit_plugin_toggle_result
from imbue.mngr.cli.plugin import _gather_plugin_info
from imbue.mngr.cli.plugin import _get_field_value
from imbue.mngr.cli.plugin import _get_installed_package_names
from imbue.mngr.cli.plugin import _is_plugin_enabled
from imbue.mngr.cli.plugin import _parse_add_sources
from imbue.mngr.cli.plugin import _parse_fields
from imbue.mngr.cli.plugin import _parse_pypi_package_name
from imbue.mngr.cli.plugin import _parse_remove_sources
from imbue.mngr.cli.plugin import _project_to_agent_type_entries
from imbue.mngr.cli.plugin import _project_to_provider_entries
from imbue.mngr.cli.plugin import _read_package_name_from_pyproject
from imbue.mngr.cli.plugin import _validate_plugin_name_is_known
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import ConfigScope
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.config.provider_config_registry import register_provider_config
from imbue.mngr.config.provider_config_registry import reset_provider_config_registry
from imbue.mngr.errors import PluginSpecifierError
from imbue.mngr.plugins import hookspecs
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import PluginName

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
    config = MngrConfig()
    assert _is_plugin_enabled("some-plugin", config) is True


def test_is_plugin_enabled_returns_false_for_disabled_plugins_set() -> None:
    """_is_plugin_enabled should return False for plugins in disabled_plugins."""
    config = MngrConfig(disabled_plugins=frozenset({"disabled-one"}))
    assert _is_plugin_enabled("disabled-one", config) is False
    assert _is_plugin_enabled("other-plugin", config) is True


def test_is_plugin_enabled_returns_false_for_config_enabled_false() -> None:
    """_is_plugin_enabled should return False for plugins with enabled=False in plugins dict."""
    config = MngrConfig(
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
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)

    # Register some test plugins with explicit names
    class PluginZ:
        pass

    class PluginA:
        pass

    pm.register(PluginZ(), name="zebra-plugin")
    pm.register(PluginA(), name="alpha-plugin")

    config = MngrConfig()
    mngr_ctx = MngrContext(
        config=config,
        pm=pm,
        profile_dir=_fake_profile_dir(),
    )

    plugins = _gather_plugin_info(mngr_ctx)
    names = [p.name for p in plugins]
    assert names == sorted(names)
    assert "alpha-plugin" in names
    assert "zebra-plugin" in names


def test_gather_plugin_info_reflects_disabled_status() -> None:
    """_gather_plugin_info should mark disabled plugins correctly."""
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)

    class MyPlugin:
        pass

    pm.register(MyPlugin(), name="my-plugin")

    config = MngrConfig(disabled_plugins=frozenset({"my-plugin"}))
    mngr_ctx = MngrContext(
        config=config,
        pm=pm,
        profile_dir=_fake_profile_dir(),
    )

    plugins = _gather_plugin_info(mngr_ctx)
    my_plugin = next(p for p in plugins if p.name == "my-plugin")
    assert my_plugin.is_enabled is False


def test_gather_plugin_info_reports_blocked_plugin_as_disabled() -> None:
    """A blocked opt-in plugin must report disabled even when config does not list it.

    Opt-in plugins (e.g. claude_subagent_proxy) are blocked in
    create_plugin_manager via read_disabled_plugins() but never reach
    config.disabled_plugins. pluggy still lists the blocked name with a None
    plugin object, so without consulting pm.is_blocked() the plugin would be
    mislabeled enabled. This asserts the reported state matches the block state.
    """
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)

    # Block a name without registering it and without listing it in config --
    # exactly the opt-in-plugin shape.
    pm.set_blocked("opt-in-plugin")
    assert pm.is_blocked("opt-in-plugin")

    config = MngrConfig()
    mngr_ctx = MngrContext(
        config=config,
        pm=pm,
        profile_dir=_fake_profile_dir(),
    )

    plugins = _gather_plugin_info(mngr_ctx)
    opt_in = next(p for p in plugins if p.name == "opt-in-plugin")
    assert opt_in.is_enabled is False


def test_gather_plugin_info_reports_unblocked_plugin_as_enabled() -> None:
    """A registered, unblocked plugin not listed as disabled reports enabled.

    The complement of the blocked case: when an opt-in plugin is explicitly
    enabled it is registered and not blocked, so it must report enabled=true.
    """
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)

    class OptInPlugin:
        pass

    pm.register(OptInPlugin(), name="opt-in-plugin")
    assert not pm.is_blocked("opt-in-plugin")

    config = MngrConfig()
    mngr_ctx = MngrContext(
        config=config,
        pm=pm,
        profile_dir=_fake_profile_dir(),
    )

    plugins = _gather_plugin_info(mngr_ctx)
    opt_in = next(p for p in plugins if p.name == "opt-in-plugin")
    assert opt_in.is_enabled is True


def test_gather_plugin_info_skips_internal_plugins() -> None:
    """_gather_plugin_info should skip plugins with names starting with underscore."""
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)

    class InternalPlugin:
        pass

    class PublicPlugin:
        pass

    pm.register(InternalPlugin(), name="_internal")
    pm.register(PublicPlugin(), name="public-plugin")

    config = MngrConfig()
    mngr_ctx = MngrContext(
        config=config,
        pm=pm,
        profile_dir=_fake_profile_dir(),
    )

    plugins = _gather_plugin_info(mngr_ctx)
    names = [p.name for p in plugins]
    assert "_internal" not in names
    assert "public-plugin" in names


def _fake_profile_dir() -> Path:
    """Return a fake profile directory path for testing."""
    return Path("/tmp/fake-mngr-profile")


# =============================================================================
# Tests for _validate_plugin_name_is_known
# =============================================================================


def test_validate_plugin_name_is_known_no_warning_for_known() -> None:
    """_validate_plugin_name_is_known should not warn for a known plugin."""
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)

    class MyPlugin:
        pass

    pm.register(MyPlugin(), name="known-plugin")

    mngr_ctx = MngrContext(
        config=MngrConfig(),
        pm=pm,
        profile_dir=_fake_profile_dir(),
    )

    warnings: list[str] = []
    sink_id = logger.add(lambda msg: warnings.append(str(msg)), level="WARNING")
    try:
        _validate_plugin_name_is_known("known-plugin", mngr_ctx)
    finally:
        logger.remove(sink_id)

    assert not any("not currently registered" in w for w in warnings)


@pytest.mark.allow_warnings(match=r"Plugin '.*' is not currently registered")
def test_validate_plugin_name_is_known_warns_for_unknown() -> None:
    """_validate_plugin_name_is_known should warn for an unknown plugin."""
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)

    mngr_ctx = MngrContext(
        config=MngrConfig(),
        pm=pm,
        profile_dir=_fake_profile_dir(),
    )

    warnings: list[str] = []
    sink_id = logger.add(lambda msg: warnings.append(str(msg)), level="WARNING")
    try:
        _validate_plugin_name_is_known("nonexistent-plugin", mngr_ctx)
    finally:
        logger.remove(sink_id)

    assert any("not currently registered" in w for w in warnings)


# =============================================================================
# Tests for _emit_plugin_toggle_result
# =============================================================================


def test_emit_plugin_toggle_result_json_enable(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_plugin_toggle_result should output valid JSON for enable."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    config_path = Path("/tmp/test/.mngr/settings.toml")

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
    config_path = Path("/tmp/test/.mngr/settings.toml")

    _emit_plugin_toggle_result("modal", False, ConfigScope.PROJECT, config_path, output_opts)

    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["plugin"] == "modal"
    assert data["enabled"] is False


def test_emit_plugin_toggle_result_jsonl_has_event_type(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_plugin_toggle_result with JSONL should include event type."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    config_path = Path("/tmp/test/.mngr/settings.toml")

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
    assert _parse_pypi_package_name("imbue-mngr-opencode") == "imbue-mngr-opencode"


def test_parse_pypi_package_name_name_with_version() -> None:
    """_parse_pypi_package_name should return the package name for specifiers with versions."""
    assert _parse_pypi_package_name("imbue-mngr-opencode>=1.0") == "imbue-mngr-opencode"


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
                        {"name": "mngr", "version": "1.0.0"},
                        {"name": "imbue-mngr-opencode", "version": "0.1.0"},
                        {"name": "pluggy", "version": "1.5.0"},
                    ]
                )

            return Result()

    names = _get_installed_package_names(FakeConcurrencyGroup())
    assert names == {"mngr", "imbue-mngr-opencode", "pluggy"}


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
    _emit_plugin_add_result("imbue-mngr-opencode", "imbue-mngr-opencode", True, output_opts)

    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["specifier"] == "imbue-mngr-opencode"
    assert data["package"] == "imbue-mngr-opencode"
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
    _emit_plugin_remove_result("imbue-mngr-opencode", output_opts)

    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["package"] == "imbue-mngr-opencode"


def test_emit_plugin_remove_result_jsonl_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_plugin_remove_result with JSONL format should include event type."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    _emit_plugin_remove_result("imbue-mngr-opencode", output_opts)

    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "plugin_removed"
    assert data["package"] == "imbue-mngr-opencode"


# =============================================================================
# Helpers for _parse_add_sources / _parse_remove_sources tests
# =============================================================================


def _make_plugin_cli_options(
    names: tuple[str, ...] = (),
    path: tuple[str, ...] = (),
    git: tuple[str, ...] = (),
) -> PluginCliOptions:
    """Create a PluginCliOptions with the given source fields and minimal defaults."""
    return PluginCliOptions(
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
        names=names,
        path=path,
        git=git,
    )


# =============================================================================
# Tests for _parse_add_sources
# =============================================================================


def test_parse_add_sources_no_source_raises_abort() -> None:
    """_parse_add_sources should raise AbortError when no source is provided."""
    opts = _make_plugin_cli_options()
    with pytest.raises(AbortError, match="Provide at least one of NAME, --path, or --git"):
        _parse_add_sources(opts)


def test_parse_add_sources_valid_pypi_name() -> None:
    """_parse_add_sources should return a list with _PypiSource for a valid PyPI name."""
    opts = _make_plugin_cli_options(names=("imbue-mngr-opencode",))
    sources = _parse_add_sources(opts)
    assert len(sources) == 1
    assert isinstance(sources[0], _PypiSource)
    assert sources[0].name == "imbue-mngr-opencode"


def test_parse_add_sources_valid_pypi_name_with_version() -> None:
    """_parse_add_sources should return a list with _PypiSource for a name with version constraint."""
    opts = _make_plugin_cli_options(names=("imbue-mngr-opencode>=1.0",))
    sources = _parse_add_sources(opts)
    assert len(sources) == 1
    assert isinstance(sources[0], _PypiSource)
    assert sources[0].name == "imbue-mngr-opencode>=1.0"


def test_parse_add_sources_multiple_names() -> None:
    """_parse_add_sources should return multiple _PypiSource for multiple names."""
    opts = _make_plugin_cli_options(names=("pkg-a", "pkg-b"))
    sources = _parse_add_sources(opts)
    assert len(sources) == 2
    assert isinstance(sources[0], _PypiSource)
    assert isinstance(sources[1], _PypiSource)
    assert sources[0].name == "pkg-a"
    assert sources[1].name == "pkg-b"


def test_parse_add_sources_single_path() -> None:
    """_parse_add_sources should return a list with _PathSource for a single path."""
    opts = _make_plugin_cli_options(path=("./my-plugin",))
    sources = _parse_add_sources(opts)
    assert len(sources) == 1
    assert isinstance(sources[0], _PathSource)
    assert sources[0].path == "./my-plugin"


def test_parse_add_sources_multiple_paths() -> None:
    """_parse_add_sources should return multiple _PathSource for multiple paths."""
    opts = _make_plugin_cli_options(path=("./plugin-a", "./plugin-b", "./plugin-c"))
    sources = _parse_add_sources(opts)
    assert len(sources) == 3
    assert isinstance(sources[0], _PathSource)
    assert isinstance(sources[1], _PathSource)
    assert isinstance(sources[2], _PathSource)
    assert sources[0].path == "./plugin-a"
    assert sources[1].path == "./plugin-b"
    assert sources[2].path == "./plugin-c"


def test_parse_add_sources_valid_git_url() -> None:
    """_parse_add_sources should return a list with _GitSource for a git URL."""
    opts = _make_plugin_cli_options(git=("https://github.com/user/repo.git",))
    sources = _parse_add_sources(opts)
    assert len(sources) == 1
    assert isinstance(sources[0], _GitSource)
    assert sources[0].url == "https://github.com/user/repo.git"


def test_parse_add_sources_multiple_git_urls() -> None:
    """_parse_add_sources should return multiple _GitSource for multiple git URLs."""
    opts = _make_plugin_cli_options(git=("https://example.com/a.git", "https://example.com/b.git"))
    sources = _parse_add_sources(opts)
    assert len(sources) == 2
    assert isinstance(sources[0], _GitSource)
    assert isinstance(sources[1], _GitSource)
    assert sources[0].url == "https://example.com/a.git"
    assert sources[1].url == "https://example.com/b.git"


def test_parse_add_sources_mixed_source_types() -> None:
    """_parse_add_sources should combine all source types into one list."""
    opts = _make_plugin_cli_options(
        names=("pkg-a",),
        path=("./local-b",),
        git=("https://example.com/c.git",),
    )
    sources = _parse_add_sources(opts)
    assert len(sources) == 3
    assert isinstance(sources[0], _PypiSource)
    assert isinstance(sources[1], _PathSource)
    assert isinstance(sources[2], _GitSource)
    assert sources[0].name == "pkg-a"
    assert sources[1].path == "./local-b"
    assert sources[2].url == "https://example.com/c.git"


def test_parse_add_sources_invalid_name_raises_abort() -> None:
    """_parse_add_sources should raise AbortError for an invalid package name."""
    opts = _make_plugin_cli_options(names=("not a valid!!spec$$",))
    with pytest.raises(AbortError, match="Invalid package name"):
        _parse_add_sources(opts)


# =============================================================================
# Tests for _parse_remove_sources
# =============================================================================


def test_parse_remove_sources_no_source_raises_abort() -> None:
    """_parse_remove_sources should raise AbortError when no source is provided."""
    opts = _make_plugin_cli_options()
    with pytest.raises(AbortError, match="Provide at least one of NAME or --path"):
        _parse_remove_sources(opts)


def test_parse_remove_sources_valid_pypi_name() -> None:
    """_parse_remove_sources should return a list with _PypiSource for a valid PyPI name."""
    opts = _make_plugin_cli_options(names=("imbue-mngr-opencode",))
    sources = _parse_remove_sources(opts)
    assert len(sources) == 1
    assert isinstance(sources[0], _PypiSource)
    assert sources[0].name == "imbue-mngr-opencode"


def test_parse_remove_sources_multiple_names() -> None:
    """_parse_remove_sources should return multiple _PypiSource for multiple names."""
    opts = _make_plugin_cli_options(names=("pkg-a", "pkg-b"))
    sources = _parse_remove_sources(opts)
    assert len(sources) == 2
    assert isinstance(sources[0], _PypiSource)
    assert isinstance(sources[1], _PypiSource)
    assert sources[0].name == "pkg-a"
    assert sources[1].name == "pkg-b"


def test_parse_remove_sources_single_path() -> None:
    """_parse_remove_sources should return a list with _PathSource for a path."""
    opts = _make_plugin_cli_options(path=("./my-plugin",))
    sources = _parse_remove_sources(opts)
    assert len(sources) == 1
    assert isinstance(sources[0], _PathSource)
    assert sources[0].path == "./my-plugin"


def test_parse_remove_sources_multiple_paths() -> None:
    """_parse_remove_sources should return multiple _PathSource for multiple paths."""
    opts = _make_plugin_cli_options(path=("./plugin-a", "./plugin-b"))
    sources = _parse_remove_sources(opts)
    assert len(sources) == 2
    assert isinstance(sources[0], _PathSource)
    assert isinstance(sources[1], _PathSource)
    assert sources[0].path == "./plugin-a"
    assert sources[1].path == "./plugin-b"


def test_parse_remove_sources_mixed_names_and_paths() -> None:
    """_parse_remove_sources should combine names and paths into one list."""
    opts = _make_plugin_cli_options(names=("pkg-a",), path=("./local-b",))
    sources = _parse_remove_sources(opts)
    assert len(sources) == 2
    assert isinstance(sources[0], _PypiSource)
    assert isinstance(sources[1], _PathSource)
    assert sources[0].name == "pkg-a"
    assert sources[1].path == "./local-b"


def test_parse_remove_sources_invalid_name_raises_abort() -> None:
    """_parse_remove_sources should raise AbortError for an invalid package name."""
    opts = _make_plugin_cli_options(names=("not a valid!!spec$$",))
    with pytest.raises(AbortError, match="Invalid package name"):
        _parse_remove_sources(opts)


# =============================================================================
# Tests for _project_to_agent_type_entries (--kind agent-type filter)
# =============================================================================


def test_project_to_agent_type_entries_keeps_existing_metadata_when_names_match() -> None:
    """When a plugin's entry-point name equals an agent-type name, reuse its full PluginInfo.

    Most agent-type plugins (claude, opencode, codex) follow the convention of
    naming their entry point identically to the registered agent type. For
    those, we want the version/description we already gathered to flow through.
    """
    plugins = [
        PluginInfo(name="codex", version="1.2.3", description="Codex agent", is_enabled=True),
        PluginInfo(name="some-unrelated-plugin", version="9.0", description="Other", is_enabled=True),
    ]
    # No user-defined agent types.
    config = MngrConfig()

    result = _project_to_agent_type_entries(plugins, config)

    by_name = {p.name: p for p in result}
    # codex is a recommended catalog agent type (enabled by default), so it must be present.
    assert "codex" in by_name
    # Metadata must come through from the existing PluginInfo entry.
    assert by_name["codex"].version == "1.2.3"
    assert by_name["codex"].description == "Codex agent"
    # The unrelated plugin must NOT be in the result.
    assert "some-unrelated-plugin" not in by_name


def test_project_to_agent_type_entries_synthesizes_user_config_defined_types() -> None:
    """Agent types defined under [agent_types.X] in user config appear with a synthesized PluginInfo.

    These are not pluggy plugins (no entry point), so there is nothing in
    ``plugins`` to reuse metadata from -- we synthesize a minimal entry.
    """
    plugins = [
        PluginInfo(name="some-unrelated-plugin", version="9.0", description="Other", is_enabled=True),
    ]
    config = MngrConfig(
        agent_types={
            AgentTypeName("only-in-config"): AgentTypeConfig(parent_type=AgentTypeName("codex")),
        },
    )

    result = _project_to_agent_type_entries(plugins, config)

    by_name = {p.name: p for p in result}
    # The user-config-defined type appears even though no plugin has that name.
    assert "only-in-config" in by_name
    # Synthesized entries have no plugin-level metadata.
    synthetic = by_name["only-in-config"]
    assert synthetic.version is None
    assert synthetic.description is None
    assert synthetic.is_enabled is True


def test_project_to_agent_type_entries_synthesizes_when_plugin_entry_point_name_differs_from_agent_type_name() -> None:
    """Agent-type names that don't match any plugin entry-point name must still appear.

    The pi_coding plugin (entry-point name 'pi_coding') registers an agent
    type named 'pi-coding' (with a hyphen). 'pi-coding' is in
    ``list_available_agent_types(config)`` but not in any plugin
    entry-point name; the projection must synthesize a fresh PluginInfo
    for it rather than dropping it. Without this, install.sh would never
    offer pi-coding as a default even when the pi_coding plugin is the
    only agent-type plugin installed.

    We register an agent class with a hyphenated name to simulate this
    shape without depending on the pi_coding plugin being installed.
    """
    _register_agent("name-with-hyphen", agent_class=BaseAgent)
    plugins = [
        # The "plugin entry-point" name is the underscored form.
        PluginInfo(name="name_with_hyphen", version="1.0", description="Hyphen plugin", is_enabled=True),
    ]
    config = MngrConfig()

    result = _project_to_agent_type_entries(plugins, config)

    by_name = {p.name: p for p in result}
    # The agent-type name (with hyphen) must be present despite the
    # entry-point name (with underscore) not matching.
    assert "name-with-hyphen" in by_name
    # Synthesized entry, since the names don't match.
    assert by_name["name-with-hyphen"].version is None
    assert by_name["name-with-hyphen"].description is None
    assert by_name["name-with-hyphen"].is_enabled is True


def test_project_to_agent_type_entries_returns_sorted_output() -> None:
    """Output must be sorted by name -- the install.sh menu and any human-readable display rely on it."""
    plugins: list[PluginInfo] = []
    config = MngrConfig(
        agent_types={
            AgentTypeName("zzz"): AgentTypeConfig(parent_type=AgentTypeName("codex")),
            AgentTypeName("aaa"): AgentTypeConfig(parent_type=AgentTypeName("codex")),
        },
    )

    result = _project_to_agent_type_entries(plugins, config)

    names = [p.name for p in result]
    assert names == sorted(names)
    assert "aaa" in names
    assert "zzz" in names


def test_project_to_agent_type_entries_emits_every_available_type() -> None:
    """Every name in list_available_agent_types(config) must be emitted.

    The projection's job is to surface the canonical set of agent types.
    Filtering by enable/disable happens upstream via ``pm.set_blocked``:
    plugins disabled in config are blocked before entry-points load, so
    their ``register_agent_type`` hookimpl never fires and their types
    are absent from ``list_available_agent_types(config)`` already.
    The projection therefore must not apply a second filter that could
    drop registered types.
    """
    # 'codex' (a recommended catalog plugin, enabled by default) and 'command'
    # (registered in core) are both available agent types, so
    # ``list_available_agent_types(MngrConfig())`` will include both.
    # We deliberately omit codex from the input ``plugins`` list to make
    # sure the projection still emits it -- the input is consulted only
    # for metadata, not for filtering.
    plugins = [
        PluginInfo(name="command", version=None, description=None, is_enabled=True),
    ]
    config = MngrConfig()

    result = _project_to_agent_type_entries(plugins, config)

    names = {p.name for p in result}
    # Both registered agent types appear, even though one was missing from `plugins`.
    assert "codex" in names
    assert "command" in names


def test_project_to_agent_type_entries_keeps_user_config_types_when_no_plugins_listed() -> None:
    """User-config-defined agent types are not pluggy plugins and must always appear.

    User-config-defined types under [agent_types.X] are not registered by
    any plugin, so they must always be surfaced -- they have no enable
    state to honor.
    """
    plugins: list[PluginInfo] = []
    config = MngrConfig(
        agent_types={
            AgentTypeName("my-custom"): AgentTypeConfig(parent_type=AgentTypeName("codex")),
        },
    )

    result = _project_to_agent_type_entries(plugins, config)

    names = {p.name for p in result}
    assert "my-custom" in names


# =============================================================================
# Tests for _project_to_provider_entries (--kind provider filter)
# =============================================================================


def test_project_to_provider_entries_keeps_existing_metadata_when_names_match() -> None:
    """A registered provider backend whose name matches a plugin entry-point should reuse metadata."""
    reset_provider_config_registry()
    try:
        register_provider_config("docker", ProviderInstanceConfig)
        plugins = [
            PluginInfo(name="docker", version="1.2.3", description="Docker backend", is_enabled=True),
            PluginInfo(name="some-unrelated-plugin", version="9.0", description="Other", is_enabled=True),
        ]

        result = _project_to_provider_entries(plugins)

        by_name = {p.name: p for p in result}
        assert "docker" in by_name
        assert by_name["docker"].version == "1.2.3"
        assert by_name["docker"].description == "Docker backend"
        assert "some-unrelated-plugin" not in by_name
    finally:
        reset_provider_config_registry()


def test_project_to_provider_entries_returns_empty_when_no_backends_registered() -> None:
    """Registry empty -> output empty, regardless of input plugins."""
    reset_provider_config_registry()
    try:
        plugins = [
            PluginInfo(name="docker", version="1.0", description="x", is_enabled=True),
        ]

        result = _project_to_provider_entries(plugins)

        assert result == []
    finally:
        reset_provider_config_registry()
