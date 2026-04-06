"""Unit tests for the schedule remove command."""

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_schedule.data_types import ScheduleTriggerDefinition
from imbue.mngr_schedule.data_types import ScheduledMngrCommand
from imbue.mngr_schedule.implementations.local.deploy import deploy_local_schedule
from imbue.mngr_schedule.implementations.local.deploy import get_local_schedule_creation_record
from imbue.mngr_schedule.implementations.local.deploy import get_local_trigger_run_script
from imbue.mngr_schedule.implementations.local.deploy import remove_local_schedule


def _make_test_trigger(name: str = "test-trigger") -> ScheduleTriggerDefinition:
    return ScheduleTriggerDefinition(
        name=name,
        command=ScheduledMngrCommand.CREATE,
        args="--message hello",
        schedule_cron="0 2 * * *",
        provider="local",
    )


def _deploy(trigger: ScheduleTriggerDefinition, mngr_ctx: MngrContext) -> list[str]:
    """Deploy a trigger and return the captured crontab entries."""
    captured: list[str] = []
    deploy_local_schedule(
        trigger,
        mngr_ctx,
        crontab_reader=lambda: "",
        crontab_writer=captured.append,
        git_hash_resolver=lambda: "fakehash",
    )
    return captured


def test_remove_local_schedule_cleans_all_artifacts(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Removing a trigger should delete the crontab entry, trigger dir, and record."""
    trigger = _make_test_trigger()
    deployed_crontab = _deploy(trigger, temp_mngr_ctx)

    removed_crontab: list[str] = []
    remove_local_schedule(
        "test-trigger",
        temp_mngr_ctx,
        crontab_reader=lambda: deployed_crontab[-1],
        crontab_writer=removed_crontab.append,
    )

    # Crontab should have been updated (entry removed)
    assert len(removed_crontab) == 1
    assert "test-trigger" not in removed_crontab[0]

    # Trigger directory should be gone
    run_script = get_local_trigger_run_script(temp_mngr_ctx, "test-trigger")
    assert not run_script.parent.exists()

    # Creation record should be gone
    assert get_local_schedule_creation_record(temp_mngr_ctx, "test-trigger") is None


def test_remove_local_schedule_idempotent(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Removing a nonexistent trigger should not raise."""
    remove_local_schedule(
        "nonexistent",
        temp_mngr_ctx,
        crontab_reader=lambda: "",
        crontab_writer=lambda _: None,
    )


def test_remove_local_schedule_leaves_other_triggers(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Removing one trigger should not affect other triggers."""
    trigger_a = _make_test_trigger("trigger-a")
    trigger_b = _make_test_trigger("trigger-b")

    crontab_state = {"content": ""}

    def crontab_reader() -> str:
        return crontab_state["content"]

    def crontab_writer(content: str) -> None:
        crontab_state["content"] = content

    deploy_local_schedule(
        trigger_a,
        temp_mngr_ctx,
        crontab_reader=crontab_reader,
        crontab_writer=crontab_writer,
        git_hash_resolver=lambda: "fakehash",
    )
    deploy_local_schedule(
        trigger_b,
        temp_mngr_ctx,
        crontab_reader=crontab_reader,
        crontab_writer=crontab_writer,
        git_hash_resolver=lambda: "fakehash",
    )

    # Remove only trigger-a
    remove_local_schedule(
        "trigger-a",
        temp_mngr_ctx,
        crontab_reader=crontab_reader,
        crontab_writer=crontab_writer,
    )

    # trigger-b should still exist
    assert get_local_schedule_creation_record(temp_mngr_ctx, "trigger-b") is not None
    assert get_local_trigger_run_script(temp_mngr_ctx, "trigger-b").is_file()
    assert "trigger-b" in crontab_state["content"]

    # trigger-a should be gone
    assert get_local_schedule_creation_record(temp_mngr_ctx, "trigger-a") is None
    assert not get_local_trigger_run_script(temp_mngr_ctx, "trigger-a").parent.exists()
    assert "trigger-a" not in crontab_state["content"]
