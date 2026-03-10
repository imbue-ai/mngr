"""Unit tests for the kanpan CLI command."""

from typing import Any

import pluggy
from click.testing import CliRunner

from imbue.mng_kanpan.cli import KanpanCliOptions
from imbue.mng_kanpan.cli import kanpan


def test_kanpan_cli_options_can_be_instantiated() -> None:
    opts = KanpanCliOptions(
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
        include=(),
        exclude=(),
        project=(),
    )
    assert opts.output_format == "human"
    assert opts.include == ()
    assert opts.exclude == ()
    assert opts.project == ()


def test_kanpan_command_calls_run_kanpan(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    patched_run_kanpan: list[dict[str, Any]],
) -> None:
    """The kanpan command should call run_kanpan with the MngContext."""
    result = cli_runner.invoke(kanpan, [], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert len(patched_run_kanpan) == 1
    assert patched_run_kanpan[0]["include_filters"] == ()
    assert patched_run_kanpan[0]["exclude_filters"] == ()


def test_kanpan_command_passes_include_filters(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    patched_run_kanpan: list[dict[str, Any]],
) -> None:
    result = cli_runner.invoke(kanpan, ["--include", 'state == "RUNNING"'], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert patched_run_kanpan[0]["include_filters"] == ('state == "RUNNING"',)
    assert patched_run_kanpan[0]["exclude_filters"] == ()


def test_kanpan_command_passes_exclude_filters(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    patched_run_kanpan: list[dict[str, Any]],
) -> None:
    result = cli_runner.invoke(kanpan, ["--exclude", 'state == "DONE"'], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert patched_run_kanpan[0]["include_filters"] == ()
    assert patched_run_kanpan[0]["exclude_filters"] == ('state == "DONE"',)


def test_kanpan_command_converts_project_to_include_filter(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    patched_run_kanpan: list[dict[str, Any]],
) -> None:
    result = cli_runner.invoke(kanpan, ["--project", "mng"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert patched_run_kanpan[0]["include_filters"] == ('labels.project == "mng"',)


def test_kanpan_command_ors_multiple_projects(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    patched_run_kanpan: list[dict[str, Any]],
) -> None:
    result = cli_runner.invoke(
        kanpan, ["--project", "mng", "--project", "other"], obj=plugin_manager, catch_exceptions=False
    )
    assert result.exit_code == 0
    assert patched_run_kanpan[0]["include_filters"] == ('labels.project == "mng" || labels.project == "other"',)


def test_kanpan_command_fails_fast_on_invalid_cel(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    result = cli_runner.invoke(kanpan, ["--include", "invalid("], obj=plugin_manager)
    assert result.exit_code != 0
