from click.testing import CliRunner

from imbue.minds.main import cli


def test_cli_shows_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "deploy" in result.output
    assert "forward" in result.output
    assert "list" in result.output
    assert "update" in result.output
