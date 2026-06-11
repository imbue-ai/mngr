"""Unit tests for the kanpan CLI command."""

import json
from typing import Any

import pluggy
from click.testing import CliRunner

from imbue.mngr_kanpan.cli import KanpanCliOptions
from imbue.mngr_kanpan.cli import kanpan


def test_kanpan_cli_options_can_be_instantiated() -> None:
    opts = KanpanCliOptions(
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
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
    """The kanpan command should call run_kanpan with the MngrContext."""
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
    result = cli_runner.invoke(kanpan, ["--project", "mngr"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert patched_run_kanpan[0]["include_filters"] == ('labels.project == "mngr"',)


def test_kanpan_command_fails_fast_on_invalid_cel(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    result = cli_runner.invoke(kanpan, ["--include", "invalid("], obj=plugin_manager)
    assert result.exit_code != 0


def test_kanpan_json_format_skips_tui_and_emits_json(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    patched_run_kanpan: list[dict[str, Any]],
) -> None:
    """`--format json` prints a single board snapshot and never launches the TUI.

    The isolated test environment has no agents, so the board is empty -- but the
    output must still be a well-formed JSON object with the expected top-level keys.
    """
    result = cli_runner.invoke(kanpan, ["--format", "json"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert patched_run_kanpan == []
    payload = json.loads(result.stdout)
    assert set(payload.keys()) == {"columns", "sections", "errors", "fetch_time_seconds"}
    # Builtin columns are always present and ordered first.
    assert payload["columns"][:2] == [{"key": "name", "header": "NAME"}, {"key": "state", "header": "STATE"}]
    assert payload["sections"] == []


def test_kanpan_jsonl_format_skips_tui(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    patched_run_kanpan: list[dict[str, Any]],
) -> None:
    """`--format jsonl` also skips the TUI; an empty board yields no output lines."""
    result = cli_runner.invoke(kanpan, ["--format", "jsonl"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert patched_run_kanpan == []
    assert result.stdout.strip() == ""
