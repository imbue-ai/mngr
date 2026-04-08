import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.run import run_command


def test_run_requires_agent_type(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Running without an agent type argument should show a usage error."""
    result = cli_runner.invoke(run_command, [], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code != 0
    assert "Missing argument" in result.output
