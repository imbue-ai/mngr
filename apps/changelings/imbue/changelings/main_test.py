from click.testing import CliRunner

from imbue.changelings.main import cli


def test_cli_shows_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "deploy" in result.output
    assert "forward" in result.output
    assert "list" in result.output
    assert "update" in result.output


def test_cli_deploy_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["deploy", "--help"])

    assert result.exit_code == 0
    assert "GIT_URL" in result.output


def test_cli_forward_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["forward", "--help"])

    assert result.exit_code == 0
    assert "forwarding server" in result.output


def test_cli_verbose_flag_shown_in_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "--verbose" in result.output or "-v" in result.output


def test_cli_quiet_flag_shown_in_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "--quiet" in result.output or "-q" in result.output
