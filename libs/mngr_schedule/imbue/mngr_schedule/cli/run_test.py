"""Unit tests for the schedule run command."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import click
import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr_modal.instance import ModalProviderInstance
from imbue.mngr_schedule.cli.run import run_local_trigger
from imbue.mngr_schedule.cli.run import run_modal_trigger
from imbue.mngr_schedule.data_types import ModalScheduleCreationRecord
from imbue.mngr_schedule.data_types import ScheduleTriggerDefinition
from imbue.mngr_schedule.data_types import ScheduledMngrCommand
from imbue.mngr_schedule.implementations.local.deploy import deploy_local_schedule


def _make_test_trigger(name: str = "test-trigger", *, is_enabled: bool = True) -> ScheduleTriggerDefinition:
    return ScheduleTriggerDefinition(
        name=name,
        command=ScheduledMngrCommand.CREATE,
        args="--message hello",
        schedule_cron="0 2 * * *",
        provider="local",
        is_enabled=is_enabled,
    )


def _make_modal_record(
    trigger_name: str = "modal-trigger",
    *,
    is_enabled: bool = True,
) -> ModalScheduleCreationRecord:
    return ModalScheduleCreationRecord(
        trigger=ScheduleTriggerDefinition(
            name=trigger_name,
            command=ScheduledMngrCommand.CREATE,
            args="--message hello",
            schedule_cron="0 3 * * *",
            provider="modal",
            is_enabled=is_enabled,
        ),
        full_commandline="mngr schedule add ...",
        hostname="test-host",
        working_directory="/tmp/test",
        mngr_git_hash="abc123",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        app_name="mngr-schedule-modal-trigger",
        environment="test-env",
    )


def _deploy_trigger(
    trigger: ScheduleTriggerDefinition,
    mngr_ctx: MngrContext,
) -> None:
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
    """Running a local trigger should execute its run.sh wrapper script."""
    trigger = _make_test_trigger()
    _deploy_trigger(trigger, temp_mngr_ctx)

    captured_scripts: list[str] = []

    def fake_runner(script_path: str) -> int:
        captured_scripts.append(script_path)
        return 0

    exit_code = run_local_trigger(temp_mngr_ctx, "test-trigger", script_runner=fake_runner)

    assert exit_code == 0
    assert len(captured_scripts) == 1
    assert captured_scripts[0].endswith("run.sh")


def test_run_local_trigger_propagates_exit_code(
    temp_mngr_ctx: MngrContext,
) -> None:
    """The exit code from run.sh should be propagated."""
    trigger = _make_test_trigger()
    _deploy_trigger(trigger, temp_mngr_ctx)

    exit_code = run_local_trigger(
        temp_mngr_ctx,
        "test-trigger",
        script_runner=lambda _: 42,
    )

    assert exit_code == 42


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
    trigger = _make_test_trigger()
    _deploy_trigger(trigger, temp_mngr_ctx)

    # Delete the run.sh file
    run_script = tmp_path / ".mngr" / "schedule" / "triggers" / "test-trigger" / "run.sh"
    run_script.unlink()

    with pytest.raises(click.ClickException, match="Wrapper script not found"):
        run_local_trigger(temp_mngr_ctx, "test-trigger")


def test_run_local_trigger_disabled_still_runs(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A disabled trigger should still be run (with a warning)."""
    trigger = _make_test_trigger("disabled-trigger", is_enabled=False)
    _deploy_trigger(trigger, temp_mngr_ctx)

    was_called = {"value": False}

    def fake_runner(script_path: str) -> int:
        was_called["value"] = True
        return 0

    exit_code = run_local_trigger(temp_mngr_ctx, "disabled-trigger", script_runner=fake_runner)

    assert exit_code == 0
    assert was_called["value"] is True


# =============================================================================
# run_modal_trigger tests
# =============================================================================


def _make_stub_provider() -> ModalProviderInstance:
    """Create a stub ModalProviderInstance that is never used.

    The provider argument to run_modal_trigger is only passed through to
    record_lookup, which is injected in tests. This stub satisfies the type
    checker without requiring real Modal credentials.
    """
    return ModalProviderInstance.model_construct()


def test_run_modal_trigger_invokes_function() -> None:
    """A successful modal trigger run should invoke the function and return 0."""
    record = _make_modal_record()
    invoked_records: list[ModalScheduleCreationRecord] = []

    exit_code = run_modal_trigger(
        _make_stub_provider(),
        "modal-trigger",
        record_lookup=lambda _p, _n: record,
        trigger_invoker=lambda r: invoked_records.append(r),
    )

    assert exit_code == 0
    assert len(invoked_records) == 1
    assert invoked_records[0].app_name == "mngr-schedule-modal-trigger"


def test_run_modal_trigger_not_found_raises() -> None:
    """Requesting a nonexistent modal trigger should raise ClickException."""
    with pytest.raises(click.ClickException, match="No modal schedule record found"):
        run_modal_trigger(
            _make_stub_provider(),
            "nonexistent",
            record_lookup=lambda _p, _n: None,
        )


def test_run_modal_trigger_invocation_failure_raises() -> None:
    """If the modal invocation fails, should raise ClickException."""
    record = _make_modal_record()

    def failing_invoker(_record: ModalScheduleCreationRecord) -> None:
        raise MngrError("connection timed out")

    with pytest.raises(click.ClickException, match="Modal invocation failed"):
        run_modal_trigger(
            _make_stub_provider(),
            "modal-trigger",
            record_lookup=lambda _p, _n: record,
            trigger_invoker=failing_invoker,
        )


def test_run_modal_trigger_disabled_still_runs() -> None:
    """A disabled modal trigger should still run (with a warning)."""
    record = _make_modal_record("disabled-modal", is_enabled=False)
    invoked = {"value": False}

    def fake_invoker(_record: ModalScheduleCreationRecord) -> None:
        invoked["value"] = True

    exit_code = run_modal_trigger(
        _make_stub_provider(),
        "disabled-modal",
        record_lookup=lambda _p, _n: record,
        trigger_invoker=fake_invoker,
    )

    assert exit_code == 0
    assert invoked["value"] is True
