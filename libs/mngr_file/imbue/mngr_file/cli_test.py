from pathlib import Path
from uuid import uuid4

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr_file.cli.get import file_get
from imbue.mngr_file.cli.group import file_group
from imbue.mngr_file.cli.list import file_list
from imbue.mngr_file.cli.put import file_put
from imbue.mngr_file.testing import read_tree

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


@pytest.mark.witnesses("template-refused-where-the-outcome-is-content")
@pytest.mark.witnesses(
    "named-output-formats",
    partial="checks only that get, whose outcome is a file's bytes, refuses a template",
)
def test_get_rejects_a_format_template(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    """A template names the fields of a record, and a read's outcome is the file's own bytes.

    Rendering a template in its place would replace the content the user asked
    for rather than describe it, so ``get`` is the one subcommand that refuses
    one. Saving the read into the addressed directory makes a read that did
    happen show up as a change there, and addressing a path with no file behind
    it makes an attempted read refuse that path instead.
    """
    directory = temp_host_dir / f"template-refused-{uuid4().hex}"
    directory.mkdir()
    (directory / "f.txt").write_bytes(b"content that must not be read")
    before = read_tree(directory)

    result = cli_runner.invoke(
        file_get,
        [
            "@localhost",
            f"{directory.name}/f.txt",
            "--output",
            str(directory / "copy.txt"),
            "--format",
            "{path}",
        ],
        obj=plugin_manager,
    )

    missing_result = cli_runner.invoke(
        file_get, ["@localhost", f"{directory.name}/missing.txt", "--format", "{path}"], obj=plugin_manager
    )

    assert result.exit_code == 2, result.output
    assert b"content that must not be read" not in result.stdout_bytes
    assert read_tree(directory) == before
    assert missing_result.exit_code == 2, missing_result.output
