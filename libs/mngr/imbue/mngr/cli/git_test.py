"""Unit tests for the git push/pull CLI subcommand group."""

from click.testing import CliRunner

from imbue.mngr.cli.git import GitPullCliOptions
from imbue.mngr.cli.git import GitPushCliOptions
from imbue.mngr.main import cli
from imbue.mngr.primitives import HostLocationAddress


def test_git_push_cli_options_can_be_instantiated() -> None:
    opts = GitPushCliOptions(
        target=HostLocationAddress(),
        dry_run=False,
        start=True,
        source_branch=None,
        target_branch=None,
        uncommitted_changes="fail",
        mirror=False,
        branch=(),
        all_branches=False,
        tags=False,
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.mirror is False


def test_git_pull_cli_options_can_be_instantiated() -> None:
    opts = GitPullCliOptions(
        source=HostLocationAddress(),
        dry_run=False,
        start=True,
        source_branch=None,
        target_branch=None,
        uncommitted_changes="fail",
        branch=(),
        all_branches=False,
        tags=False,
        force_git=False,
        merge=False,
        rebase=False,
        uncommitted_source=None,
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.dry_run is False


def test_git_push_help_shows_options() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["git", "push", "--help"])
    assert result.exit_code == 0
    assert "--source-branch" in result.output
    assert "--target-branch" in result.output
    assert "--mirror" in result.output
    assert "--dry-run" in result.output


def test_git_pull_help_shows_options() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["git", "pull", "--help"])
    assert result.exit_code == 0
    assert "--source-branch" in result.output
    assert "--target-branch" in result.output
    assert "--dry-run" in result.output
