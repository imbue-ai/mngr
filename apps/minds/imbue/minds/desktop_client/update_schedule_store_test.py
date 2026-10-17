from pathlib import Path

from imbue.minds.desktop_client.update_schedule_store import UpdateScheduleStore
from imbue.mngr.primitives import AgentId


def test_armed_intents_are_reloaded_across_store_instances(tmp_path: Path) -> None:
    """The store serves reads from memory, so a relaunch must seed that memory from what the last launch wrote."""
    pinned_id, skipped_id = AgentId.generate(), AgentId.generate()
    first_store = UpdateScheduleStore(records_dir=tmp_path / "update_schedules")
    first_store.schedule(pinned_id, target_ref="minds-v0.5.0")
    first_store.schedule(skipped_id)
    first_store.record_skip(skipped_id, "WORKSPACE_UNREACHABLE")

    second_store = UpdateScheduleStore(records_dir=tmp_path / "update_schedules")
    assert {record.agent_id for record in second_store.list_records()} == {str(pinned_id), str(skipped_id)}
    pinned = second_store.read(pinned_id)
    assert pinned is not None and pinned.target_ref == "minds-v0.5.0"
    skipped = second_store.read(skipped_id)
    assert skipped is not None and skipped.last_skip_reason == "WORKSPACE_UNREACHABLE"

    # A cancel through the relaunched store reaches the disk, so a further launch does not re-arm it.
    assert second_store.cancel(pinned_id) is True
    third_store = UpdateScheduleStore(records_dir=tmp_path / "update_schedules")
    assert [record.agent_id for record in third_store.list_records()] == [str(skipped_id)]
