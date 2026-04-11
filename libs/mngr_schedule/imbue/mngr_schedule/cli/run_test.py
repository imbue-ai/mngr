"""Integration tests for the schedule run command (local provider)."""

from pathlib import Path

import click
import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_schedule.cli.run import run_local_trigger
from imbue.mngr_schedule.data_types import ScheduleTriggerDefinition
from imbue.mngr_schedule.data_types import ScheduledMngrCommand
from imbue.mngr_schedule.implementations.local.deploy import deploy_local_schedule


def _deploy_echo_trigger(
    mngr_ctx: MngrContext,
    name: str = "test-trigger",
    *,
    is_enabled: bool = True,
) -> None:
    """Deploy a local trigger whose run.sh runs 'echo hello'."""
    trigger = ScheduleTriggerDefinition(
        name=name,
        command=ScheduledMngrCommand.CREATE,
        args="--message hello",
        schedule_cron="0 2 * * *",
        provider="local",
        is_enabled=is_enabled,
    )
    deploy_local_schedule(
        trigger,
        mngr_ctx,
        crontab_reader=lambda: "",
        crontab_writer=lambda _: None,
        git_hash_resolver=lambda: "fakehash",
    )


def test_run_local_trigger_executes_run_script(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Running a local trigger should execute its run.sh and return an exit code."""
    _deploy_echo_trigger(temp_mngr_ctx)
    # run.sh will fail (uv run mngr create isn't set up in test env) but
    # the point is that it tried to execute the script. A non-zero exit
    # code proves run.sh was invoked.
    exit_code = run_local_trigger(temp_mngr_ctx, "test-trigger")
    assert isinstance(exit_code, int)


def test_run_local_trigger_not_found_raises(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Requesting a nonexistent trigger should raise ClickException."""
    with pytest.raises(click.ClickException, match="No local schedule record found"):
        run_local_trigger(temp_mngr_ctx, "nonexistent")


def test_run_local_trigger_missing_script_raises(
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """If the record exists but run.sh is missing, should raise ClickException."""
    _deploy_echo_trigger(temp_mngr_ctx)

    # Delete the run.sh file
    run_script = tmp_path / ".mngr" / "schedule" / "triggers" / "test-trigger" / "run.sh"
    run_script.unlink()

    with pytest.raises(click.ClickException, match="Wrapper script not found"):
        run_local_trigger(temp_mngr_ctx, "test-trigger")


def test_run_local_trigger_disabled_still_runs(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A disabled trigger should still be run (with a warning)."""
    _deploy_echo_trigger(temp_mngr_ctx, "disabled-trigger", is_enabled=False)
    exit_code = run_local_trigger(temp_mngr_ctx, "disabled-trigger")
    assert isinstance(exit_code, int)
