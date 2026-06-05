"""Unit tests for the rsync CLI command."""

from pathlib import Path

import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.rsync import _user_path_to_str
from imbue.mngr.cli.rsync import rsync_command
from imbue.mngr.main import cli


def test_rsync_command_is_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["rsync", "--help"])
    assert result.exit_code == 0
    assert "Rsync files" in result.output


def test_rsync_requires_two_positional_args(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """A single positional arg is a click usage error (exit 2) naming the missing arg."""
    result = cli_runner.invoke(rsync_command, ["my-agent"], obj=plugin_manager, catch_exceptions=True)
    # Click raises MissingParameter for a required argument before the body runs;
    # standalone-mode usage errors exit with code 2.
    assert result.exit_code == 2
    assert "Missing argument" in result.output
    assert "DESTINATION" in result.output


def test_rsync_rejects_local_to_local(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Two bare local paths are rejected with RsyncEndpointError (rsync.py:146-149).

    ``RsyncEndpointError`` is a ``UserInputError`` (a ClickException), so under the
    runner it renders as an ``Error:`` line and exits with code 1.
    """
    result = cli_runner.invoke(
        rsync_command,
        ["./src", "./dst"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code == 1
    assert "mngr rsync requires one of SOURCE or DESTINATION to reference an agent or remote host" in result.output


def test_user_path_to_str_appends_trailing_slash_when_flagged() -> None:
    """When the user typed a trailing ``/``, it is re-appended (Path strips it). rsync.py:41-46."""
    assert _user_path_to_str(Path("/work/src"), has_trailing_slash=True) == "/work/src/"


def test_user_path_to_str_omits_trailing_slash_when_not_flagged() -> None:
    """Without the trailing-slash flag, the path is returned verbatim. rsync.py:41-46."""
    assert _user_path_to_str(Path("/work/src"), has_trailing_slash=False) == "/work/src"


def test_user_path_to_str_does_not_double_trailing_slash() -> None:
    """An already-slash-terminated string is not given a second slash. rsync.py:44-45."""
    # ``Path("/")`` stringifies to ``"/"``; the guard avoids producing ``"//"``.
    assert _user_path_to_str(Path("/"), has_trailing_slash=True) == "/"
