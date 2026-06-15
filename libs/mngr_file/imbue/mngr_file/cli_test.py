from click.testing import CliRunner

from imbue.mngr_file.cli.get import file_get
from imbue.mngr_file.cli.group import file_group
from imbue.mngr_file.cli.list import file_list
from imbue.mngr_file.cli.put import file_put

# The --help tests below are intentionally shallow smoke checks that the expected
# options are advertised in help output. The command/subcommand registration is
# verified structurally in plugin_test.py, and option *behavior* is verified by the
# integration tests in test_file_operations.py.


def test_file_group_shows_help() -> None:
    runner = CliRunner()
    result = runner.invoke(file_group, ["--help"])
    assert result.exit_code == 0
    assert "--help" in result.output
    assert "get" in result.output
    assert "put" in result.output
    assert "list" in result.output


def test_file_get_shows_help() -> None:
    runner = CliRunner()
    result = runner.invoke(file_get, ["--help"])
    assert result.exit_code == 0
    assert "--output" in result.output
    assert "--relative-to" in result.output


def test_file_put_shows_help() -> None:
    runner = CliRunner()
    result = runner.invoke(file_put, ["--help"])
    assert result.exit_code == 0
    assert "--input" in result.output
    assert "--mode" in result.output


def test_file_list_shows_help() -> None:
    runner = CliRunner()
    result = runner.invoke(file_list, ["--help"])
    assert result.exit_code == 0
    assert "--fields" in result.output
    assert "--recursive" in result.output


def test_file_get_rejects_missing_target_and_path_with_usage_error() -> None:
    runner = CliRunner()
    result = runner.invoke(file_get, [])
    assert result.exit_code == 2
    assert "Usage:" in result.output
    assert "Missing argument" in result.output


def test_file_put_rejects_missing_target_and_path_with_usage_error() -> None:
    runner = CliRunner()
    result = runner.invoke(file_put, [])
    assert result.exit_code == 2
    assert "Usage:" in result.output
    assert "Missing argument" in result.output


def test_file_list_rejects_missing_target_with_usage_error() -> None:
    runner = CliRunner()
    result = runner.invoke(file_list, [])
    assert result.exit_code == 2
    assert "Usage:" in result.output
    assert "Missing argument" in result.output
