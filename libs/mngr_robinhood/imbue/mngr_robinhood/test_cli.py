"""Integration tests for the ``mngr robinhood`` CLI command.

These drive the real click command through the top-level ``mngr`` group via the
shared ``cli_runner`` fixture, exercising the wiring from argv ->
``setup_command_context`` -> ``partition_args`` -> exit-code mapping. They cover
the failure paths that reject before any agent is spawned, so they need no live
claude agent and stay fast and deterministic. The happy path (a real turn) is
left to a heavier acceptance test.
"""

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.main import cli
from imbue.mngr_robinhood.arg_partition import REJECTED_FLAGS
from imbue.mngr_robinhood.orchestrator import EXIT_MNGR_ERROR


def test_robinhood_is_registered_on_the_cli(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    # The plugin's register_cli_commands hook must actually wire the command
    # into the top-level group. This guards the other tests here from passing
    # for the wrong reason: click's own "No such command" error ALSO exits with
    # code 2, so an unregistered command would make the exit-code assertions
    # below pass spuriously. ``--help`` exits 0 only if the command exists.
    result = cli_runner.invoke(cli, ["robinhood", "--help"], obj=plugin_manager)
    assert result.exit_code == 0
    assert "No such command" not in result.output
    assert "robinhood" in result.output


@pytest.mark.parametrize("flag", sorted(REJECTED_FLAGS.keys()))
def test_rejected_flag_exits_with_mngr_error(
    flag: str,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """A flag in REJECTED_FLAGS must map to the mngr-error exit code (2), not a
    click usage error or a crash."""
    result = cli_runner.invoke(cli, ["robinhood", flag], obj=plugin_manager)
    assert result.exit_code == EXIT_MNGR_ERROR


def test_bad_output_format_exits_with_mngr_error(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """An unparseable ``--output-format`` value is an mngr-side input error and
    must exit with EXIT_MNGR_ERROR (2)."""
    result = cli_runner.invoke(cli, ["robinhood", "--output-format=bogus", "hi"], obj=plugin_manager)
    assert result.exit_code == EXIT_MNGR_ERROR
