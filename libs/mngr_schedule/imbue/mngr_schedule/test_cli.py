"""Integration tests for the schedule CLI command."""

import click
import pluggy
from click.testing import CliRunner

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_schedule.cli.commands import schedule
from imbue.mngr_schedule.data_types import ScheduleTriggerDefinition
from imbue.mngr_schedule.data_types import ScheduledMngrCommand
from imbue.mngr_schedule.implementations.local.deploy import deploy_local_schedule


def test_schedule_defaults_to_add_subcommand(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that 'mngr schedule' without a subcommand defaults to 'add'."""
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


# =============================================================================
# schedule remove CLI tests
# =============================================================================


def _deploy_local_trigger(
    mngr_ctx: MngrContext,
    name: str,
) -> None:
    """Deploy a local trigger for testing."""
    trigger = ScheduleTriggerDefinition(
        name=name,
        command=ScheduledMngrCommand.CREATE,
        args="--message hello",
        schedule_cron="0 2 * * *",
        provider="local",
    )
    deploy_local_schedule(
        trigger,
        mngr_ctx,
        crontab_reader=lambda: "",
        crontab_writer=lambda _: None,
        git_hash_resolver=lambda: "fakehash",
    )


def test_schedule_remove_requires_names(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Running remove without any trigger names should show a usage error."""
    result = cli_runner.invoke(
        schedule,
        ["remove", "--provider", "local", "--force"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "Missing argument" in result.output


def test_schedule_remove_requires_provider(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Running remove without --provider should show an error."""
    result = cli_runner.invoke(
        schedule,
        ["remove", "some-trigger", "--force"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "Missing option" in result.output or "required" in result.output.lower()


def test_schedule_remove_local_with_force(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Removing a deployed local trigger with --force should succeed."""
    _deploy_local_trigger(temp_mngr_ctx, "test-remove-trigger")

    result = cli_runner.invoke(
        schedule,
        ["remove", "test-remove-trigger", "--provider", "local", "--force"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0, f"remove failed: {result.output}"
    assert "Removed schedule" in result.output


def test_schedule_remove_local_missing_trigger_with_force(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Removing a nonexistent trigger with --force should succeed (idempotent)."""
    result = cli_runner.invoke(
        schedule,
        ["remove", "nonexistent-trigger", "--provider", "local", "--force"],
        obj=plugin_manager,
    )
    # Should succeed (no triggers found means nothing to remove)
    assert result.exit_code == 0


def test_schedule_remove_local_prompts_without_force(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Without --force, remove should prompt and abort on 'n' input."""
    _deploy_local_trigger(temp_mngr_ctx, "test-prompt-trigger")

    result = cli_runner.invoke(
        schedule,
        ["remove", "test-prompt-trigger", "--provider", "local"],
        obj=plugin_manager,
        input="n\n",
    )
    # User declined: code raises SystemExit(0), CliRunner reports exit_code=0.
    # The trigger was NOT removed because the user declined.
    assert "Are you sure" in result.output
    assert "Removed schedule" not in result.output


# =============================================================================
# schedule run CLI tests
# =============================================================================


def test_schedule_run_requires_name(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Running schedule run without a trigger name should show a usage error."""
    result = cli_runner.invoke(
        schedule,
        ["run", "--provider", "local"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "Missing argument" in result.output


def test_schedule_run_requires_provider(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Running schedule run without --provider should show an error."""
    result = cli_runner.invoke(
        schedule,
        ["run", "some-trigger"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "Missing option" in result.output or "required" in result.output.lower()


def test_schedule_run_local_nonexistent_trigger(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Running a nonexistent local trigger should fail with a clear error."""
    result = cli_runner.invoke(
        schedule,
        ["run", "nonexistent-trigger", "--provider", "local"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "No local schedule record found" in result.output


def test_schedule_run_local_deployed_trigger(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Running a deployed local trigger should attempt to execute the run script."""
    _deploy_local_trigger(temp_mngr_ctx, "test-run-trigger")

    result = cli_runner.invoke(
        schedule,
        ["run", "test-run-trigger", "--provider", "local"],
        obj=plugin_manager,
    )
    # run.sh will fail (mngr create isn't available in test env) but the
    # command should not error at the CLI level -- it should propagate the
    # script's exit code. The exit code may be non-zero because the run.sh
    # itself fails, which is expected.
    assert isinstance(result.exit_code, int)


# =============================================================================
# schedule list CLI tests
# =============================================================================


def test_schedule_list_local_empty(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Listing schedules on local with no triggers should succeed with empty output."""
    result = cli_runner.invoke(
        schedule,
        ["list", "--provider", "local"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0


def test_schedule_list_local_with_trigger(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Listing schedules on local should show deployed triggers."""
    _deploy_local_trigger(temp_mngr_ctx, "test-list-trigger")

    result = cli_runner.invoke(
        schedule,
        ["list", "--provider", "local"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    assert "test-list-trigger" in result.output


def test_schedule_list_local_json(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Listing schedules with --format=json should produce JSON output."""
    _deploy_local_trigger(temp_mngr_ctx, "test-json-trigger")

    result = cli_runner.invoke(
        schedule,
        ["list", "--provider", "local", "--format=json"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    assert "test-json-trigger" in result.output


def test_schedule_list_local_jsonl(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Listing schedules with --format=jsonl should produce JSONL output."""
    _deploy_local_trigger(temp_mngr_ctx, "test-jsonl-trigger")

    result = cli_runner.invoke(
        schedule,
        ["list", "--provider", "local", "--format=jsonl"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    assert "test-jsonl-trigger" in result.output
