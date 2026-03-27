"""Unit tests for the push CLI command."""

import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.push import PushCliOptions
from imbue.mngr.cli.push import push


def test_push_cli_options_can_be_instantiated() -> None:
    """Test that PushCliOptions can be instantiated with all required fields."""
    opts = PushCliOptions(
        target_pos=None,
        source_pos=None,
        target=None,
        target_agent=None,
        target_host=None,
        target_path=None,
        source=None,
        dry_run=False,
        stop=False,
        delete=False,
        sync_mode="files",
        exclude=(),
        uncommitted_changes="fail",
        source_branch=None,
        mirror=False,
        rsync_only=False,
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.sync_mode == "files"
    assert opts.dry_run is False
    assert opts.delete is False
    assert opts.uncommitted_changes == "fail"


def test_push_requires_target(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that push requires a target."""
    result = cli_runner.invoke(
        push,
        [],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0


def test_push_source_branch_requires_git_mode(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --source-branch requires --sync-mode=git."""
    result = cli_runner.invoke(
        push,
        ["nonexistent-push-agent-123", "--source-branch", "main"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "--source-branch can only be used with --sync-mode=git" in result.output


def test_push_mirror_requires_git_mode(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --mirror requires --sync-mode=git."""
    result = cli_runner.invoke(
        push,
        ["nonexistent-push-agent-124", "--mirror"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "--mirror can only be used with --sync-mode=git" in result.output


def test_push_rsync_only_with_source_branch_rejected(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --rsync-only with --source-branch (in git mode) is rejected."""
    result = cli_runner.invoke(
        push,
        ["nonexistent-push-agent-125", "--rsync-only", "--source-branch", "main", "--sync-mode", "git"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0


def test_push_rsync_only_with_mirror_rejected(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --rsync-only with --mirror (in git mode) is rejected."""
    result = cli_runner.invoke(
        push,
        ["nonexistent-push-agent-126", "--rsync-only", "--mirror", "--sync-mode", "git"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
