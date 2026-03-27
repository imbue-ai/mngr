"""Integration tests for the schedule CLI command."""

import click
import pluggy
from click.testing import CliRunner

from imbue.mng_schedule.cli.commands import schedule


def test_schedule_defaults_to_add_subcommand(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that 'mng schedule' without a subcommand defaults to 'add'."""
    result = cli_runner.invoke(
        schedule,
        [],
        obj=plugin_manager,
    )
    # Should fail for missing --schedule (from add), not "No such command"
    assert result.exit_code != 0
    assert "--schedule is required" in result.output


def test_schedule_add_defaults_command_to_create(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that schedule add defaults --command to 'create' when not specified."""
    result = cli_runner.invoke(
        schedule,
        ["add"],
        obj=plugin_manager,
    )
    # Should fail for missing --schedule, not missing --command
    assert result.exit_code != 0
    assert "--schedule is required" in result.output


def test_schedule_add_requires_schedule(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that schedule add requires --schedule."""
    result = cli_runner.invoke(
        schedule,
        ["add", "--command", "create"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "--schedule is required" in result.output


def test_schedule_add_requires_provider(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that schedule add requires --provider."""
    result = cli_runner.invoke(
        schedule,
        ["add", "--command", "create", "--schedule", "0 2 * * *"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "--provider is required" in result.output


def test_schedule_add_rejects_unsupported_provider(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that schedule add rejects providers that are not local or modal."""
    result = cli_runner.invoke(
        schedule,
        [
            "add",
            "--command",
            "create",
            "--schedule",
            "0 2 * * *",
            "--provider",
            "ssh",
        ],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "not supported for schedules" in result.output


def test_schedule_update_raises_not_implemented(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that schedule update raises NotImplementedError with shared options."""
    result = cli_runner.invoke(
        schedule,
        [
            "update",
            "--name",
            "my-trigger",
            "--disabled",
        ],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert result.exception is not None
    assert isinstance(result.exception, NotImplementedError)
    assert "schedule update is not implemented yet" in str(result.exception)


def test_schedule_add_accepts_positional_name(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that schedule add accepts name as a positional argument (no UsageError about name)."""
    result = cli_runner.invoke(
        schedule,
        ["add", "my-trigger", "--command", "create"],
        obj=plugin_manager,
    )
    # Should fail due to missing --schedule, not because of positional name
    assert result.exit_code != 0
    assert "Cannot specify both" not in result.output


def test_schedule_update_accepts_positional_name(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that schedule update accepts name as a positional argument (no UsageError)."""
    result = cli_runner.invoke(
        schedule,
        ["update", "my-trigger", "--disabled"],
        obj=plugin_manager,
    )
    assert not isinstance(result.exception, (click.UsageError, SystemExit))


def test_schedule_add_rejects_both_positional_and_option_name(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that specifying both positional name and --name is an error."""
    result = cli_runner.invoke(
        schedule,
        ["add", "pos-name", "--name", "opt-name"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "Cannot specify both" in result.output


def test_schedule_add_and_update_share_options(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that add and update accept the same trigger options."""
    shared_args = [
        "--name",
        "test-trigger",
        "--command",
        "create",
        "--schedule",
        "0 3 * * *",
        "--provider",
        "modal",
        "--verify",
        "none",
    ]

    # add will fail trying to load the modal provider in the test env
    # but it should not be a UsageError or click error
    add_result = cli_runner.invoke(
        schedule,
        ["add", *shared_args],
        obj=plugin_manager,
    )
    # Should fail with ScheduleDeployError (wrapped as ClickException), not a UsageError
    assert add_result.exit_code != 0
    assert not isinstance(add_result.exception, click.UsageError)

    update_result = cli_runner.invoke(
        schedule,
        ["update", *shared_args],
        obj=plugin_manager,
    )
    assert isinstance(update_result.exception, NotImplementedError)


def test_schedule_add_accepts_verify_none(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --verify none is accepted."""
    result = cli_runner.invoke(
        schedule,
        [
            "add",
            "--command",
            "create",
            "--schedule",
            "0 2 * * *",
            "--provider",
            "modal",
            "--verify",
            "none",
        ],
        obj=plugin_manager,
    )
    # Should fail at deploy (no git repo), NOT at verify option parsing
    assert result.exit_code != 0
    assert not isinstance(result.exception, click.UsageError)


def test_schedule_add_accepts_verify_full(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --verify full is accepted."""
    result = cli_runner.invoke(
        schedule,
        [
            "add",
            "--command",
            "create",
            "--schedule",
            "0 2 * * *",
            "--provider",
            "modal",
            "--verify",
            "full",
        ],
        obj=plugin_manager,
    )
    # Should fail at deploy (no git repo), NOT at verify option parsing
    assert result.exit_code != 0
    assert not isinstance(result.exception, click.UsageError)


def test_schedule_add_rejects_invalid_verify_value(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --verify with an invalid value is rejected by click."""
    result = cli_runner.invoke(
        schedule,
        [
            "add",
            "--command",
            "create",
            "--schedule",
            "0 2 * * *",
            "--provider",
            "modal",
            "--verify",
            "invalid",
        ],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "Invalid value" in result.output


def test_schedule_add_snapshot_raises_not_implemented(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --snapshot raises NotImplementedError."""
    result = cli_runner.invoke(
        schedule,
        [
            "add",
            "--command",
            "create",
            "--schedule",
            "0 2 * * *",
            "--provider",
            "modal",
            "--snapshot",
            "snap-123",
        ],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, NotImplementedError)
    assert "--snapshot is not yet implemented" in str(result.exception)


def test_schedule_add_full_copy_accepted(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --full-copy is accepted and does not raise NotImplementedError."""
    result = cli_runner.invoke(
        schedule,
        [
            "add",
            "--command",
            "create",
            "--schedule",
            "0 2 * * *",
            "--provider",
            "modal",
            "--full-copy",
            "--no-auto-merge",
        ],
        obj=plugin_manager,
    )
    # Should fail at deploy (provider loading), not NotImplementedError
    assert result.exit_code != 0
    assert not isinstance(result.exception, NotImplementedError)
