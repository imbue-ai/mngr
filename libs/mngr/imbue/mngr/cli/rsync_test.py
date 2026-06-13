"""Unit tests for the rsync CLI command."""

import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.rsync import RsyncCliOptions
from imbue.mngr.cli.rsync import rsync_command
from imbue.mngr.main import cli
from imbue.mngr.primitives import HostLocationAddress


def test_rsync_cli_options_can_be_instantiated() -> None:
    opts = RsyncCliOptions(
        source=HostLocationAddress(),
        destination=HostLocationAddress(),
        start=True,
        uncommitted_changes="fail",
        include_gitignored=False,
        rsync_args=(),
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.start is True
    assert opts.uncommitted_changes == "fail"


def test_rsync_command_is_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["rsync", "--help"])
    assert result.exit_code == 0
    assert "Rsync files" in result.output


def test_rsync_requires_two_positional_args(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    result = cli_runner.invoke(rsync_command, ["my-agent"], obj=plugin_manager, catch_exceptions=True)
    assert result.exit_code != 0
