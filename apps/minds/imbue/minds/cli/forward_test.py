from click.testing import CliRunner

from imbue.minds.cli_entry import cli

_RUNNER = CliRunner()


def test_forward_command_help_shows_all_options() -> None:
    result = _RUNNER.invoke(cli, ["forward", "--help"])

    assert result.exit_code == 0
    assert "desktop client" in result.output
    assert "--host" in result.output
    assert "--port" in result.output
    assert "127.0.0.1" in result.output
    assert "8420" in result.output


def test_forward_command_no_longer_accepts_data_dir() -> None:
    """--data-dir was removed; MINDS_ROOT_NAME is the single entry point."""
    result = _RUNNER.invoke(cli, ["forward", "--help"])

    assert result.exit_code == 0
    assert "--data-dir" not in result.output
