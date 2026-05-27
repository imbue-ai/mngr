"""Unit tests for the git push/pull CLI subcommand group."""

from click.testing import CliRunner

from imbue.mngr.cli.git import GitPullCliOptions
from imbue.mngr.cli.git import GitPushCliOptions
from imbue.mngr.main import cli
from imbue.mngr.primitives import HostLocationAddress


def test_git_push_cli_options_can_be_instantiated() -> None:
    opts = GitPushCliOptions(
        target=HostLocationAddress(),
        start=True,
        git_args=(),
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.start is True


def test_git_pull_cli_options_can_be_instantiated() -> None:
    opts = GitPullCliOptions(
        source=HostLocationAddress(),
        start=True,
        git_args=(),
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.start is True


def test_git_push_help_describes_passthrough() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["git", "push", "--help"])
    assert result.exit_code == 0
    assert "GIT_ARGS" in result.output or "git push" in result.output.lower()


def test_git_pull_help_describes_passthrough() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["git", "pull", "--help"])
    assert result.exit_code == 0
    assert "GIT_ARGS" in result.output or "git pull" in result.output.lower()
