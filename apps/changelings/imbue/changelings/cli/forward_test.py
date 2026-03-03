from click.testing import CliRunner

from imbue.changelings.main import cli

_RUNNER = CliRunner()


def test_forward_command_help_shows_all_options() -> None:
    result = _RUNNER.invoke(cli, ["forward", "--help"])

    assert result.exit_code == 0
    assert "forwarding server" in result.output
    assert "--host" in result.output
    assert "--port" in result.output
    assert "--data-dir" in result.output
    assert "127.0.0.1" in result.output
    assert "8420" in result.output
