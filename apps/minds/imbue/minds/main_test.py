from click.testing import CliRunner

from imbue.minds.cli_entry import cli


def test_cli_shows_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "forward" in result.output
