"""Unit tests for the kanpan CLI command."""

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
    """The kanpan command forwards --include/--exclude verbatim to run_kanpan.

    build_agent_filter_cel passes the raw --include/--exclude CEL strings
    through unchanged, so this asserts the CLI -> build_agent_filter_cel ->
    run_kanpan wiring (and the empty-args default), not the CEL translation
    itself (the non-identity translation is pinned by the --project test below).
    """
    result = cli_runner.invoke(kanpan, [], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert len(patched_run_kanpan) == 1
    assert patched_run_kanpan[0]["include_filters"] == ()
    assert patched_run_kanpan[0]["exclude_filters"] == ()

    result = cli_runner.invoke(
        kanpan,
        ["--include", 'state == "RUNNING"', "--exclude", 'state == "DONE"'],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert len(patched_run_kanpan) == 2
    assert patched_run_kanpan[1]["include_filters"] == ('state == "RUNNING"',)
    assert patched_run_kanpan[1]["exclude_filters"] == ('state == "DONE"',)


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
    patched_run_kanpan: list[dict[str, Any]],
) -> None:
    """An invalid --include CEL expression aborts before run_kanpan is reached.

    Requesting patched_run_kanpan ensures that if CEL validation were removed,
    the command would fall through to (the stubbed) run_kanpan and this test
    would fail on the recorder length rather than silently passing on a real
    TUI crash. The output assertion pins the specific CEL-compile error surface
    (see compile_cel_filters in cel_utils.py) so the test cannot pass for an
    unrelated nonzero exit (missing fixture, import/plugin-load failure, etc.).
    """
    result = cli_runner.invoke(kanpan, ["--include", "invalid("], obj=plugin_manager)
    assert result.exit_code != 0
    assert len(patched_run_kanpan) == 0
    assert "Invalid include filter expression 'invalid('" in result.output
