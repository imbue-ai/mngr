"""Unit tests for pull CLI command."""

import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.pull import PullCliOptions
from imbue.mngr.cli.pull import pull
from imbue.mngr.main import cli


def test_pull_cli_options_can_be_instantiated() -> None:
    """Test that PullCliOptions can be instantiated with all required fields."""
    opts = PullCliOptions(
        source_pos=None,
        destination_pos=None,
        source=None,
        source_agent=None,
        source_host=None,
        source_path=None,
        destination=None,
        dry_run=False,
        start=True,
        stop=False,
        delete=False,
        sync_mode="files",
        exclude=(),
        uncommitted_changes="fail",
        target_branch=None,
        target=None,
        target_agent=None,
        target_host=None,
        target_path=None,
        stdin=False,
        include=(),
        include_gitignored=False,
        include_file=None,
        exclude_file=None,
        rsync_arg=(),
        rsync_args=None,
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
    assert opts.sync_mode == "files"
    assert opts.dry_run is False
    assert opts.delete is False
    assert opts.uncommitted_changes == "fail"


def test_pull_cli_options_has_all_fields() -> None:
    """Test that PullCliOptions has all expected fields."""
    assert hasattr(PullCliOptions, "__annotations__")
    annotations = PullCliOptions.__annotations__
    assert "source" in annotations
    assert "source_agent" in annotations
    assert "source_host" in annotations
    assert "source_path" in annotations
    assert "destination" in annotations
    assert "dry_run" in annotations
    assert "stop" in annotations
    assert "delete" in annotations
    assert "sync_mode" in annotations
    assert "exclude" in annotations
    assert "target_branch" in annotations


def test_pull_command_is_registered() -> None:
    """Test that pull command is registered in the CLI group."""
    runner = CliRunner()
    result = runner.invoke(cli, ["pull", "--help"])
    assert result.exit_code == 0
    assert "Pull files or git commits from an agent" in result.output


def test_pull_command_help_shows_options() -> None:
    """Test that pull --help shows all options."""
    runner = CliRunner()
    result = runner.invoke(cli, ["pull", "--help"])
    assert result.exit_code == 0
    assert "--source-agent" in result.output
    assert "--source-path" in result.output
    assert "--destination" in result.output
    assert "--dry-run" in result.output
    assert "--stop" in result.output
    assert "--delete" in result.output
    assert "--sync-mode" in result.output
    assert "--exclude" in result.output


def test_pull_command_sync_mode_choices() -> None:
    """Test that sync-mode shows valid choices."""
    runner = CliRunner()
    result = runner.invoke(cli, ["pull", "--help"])
    assert result.exit_code == 0
    assert "files" in result.output
    assert "git" in result.output
    assert "full" in result.output


def test_pull_target_branch_requires_git_mode(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --target-branch requires --sync-mode=git."""
    result = cli_runner.invoke(
        pull,
        ["nonexistent-pull-agent-222", "--target-branch", "main"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "--target-branch can only be used with --sync-mode=git" in result.output
