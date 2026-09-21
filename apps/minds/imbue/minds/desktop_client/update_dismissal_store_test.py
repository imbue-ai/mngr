from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.minds.desktop_client.update_dismissal_store import UpdateDismissalRecord
from imbue.minds.desktop_client.update_dismissal_store import UpdateDismissalStore
from imbue.mngr.primitives import AgentId


def test_a_file_that_is_not_a_workspaces_record_costs_no_other_workspace_its_dismissals(tmp_path: Path) -> None:
    """The whole directory is read at once, and the file's name is the workspace it answers for."""
    records_dir = tmp_path / "update_dismissals"
    dismissed_id, unaffected_id, ghost_id = AgentId.generate(), AgentId.generate(), AgentId.generate()
    dismissed_at = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)
    first_store = UpdateDismissalStore(records_dir=records_dir)
    first_store.dismiss_note(dismissed_id, dismissed_at)
    first_store.dismiss_outcome(unaffected_id, dismissed_at)
    (records_dir / f"{AgentId.generate()}.json").write_text("{not json", encoding="utf-8")
    # A well-formed record under a name that is not an agent id: the name is the
    # index, so this is not the ghost workspace's dismissal.
    (records_dir / "scratch.json").write_text(
        UpdateDismissalRecord(agent_id=str(ghost_id), note_run_started_at=dismissed_at).model_dump_json(),
        encoding="utf-8",
    )

    relaunched = UpdateDismissalStore(records_dir=records_dir)
    assert relaunched.read(dismissed_id).note_run_started_at == dismissed_at
    assert relaunched.read(unaffected_id).outcome_run_started_at == dismissed_at
    assert relaunched.read(ghost_id).note_run_started_at is None
