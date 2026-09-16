"""Unit tests for the record of which folders to keep synced."""

from pathlib import Path

from imbue.minds.desktop_client.folder_sync_settings import FolderSyncConflict
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncDirection
from imbue.minds.desktop_client.folder_sync_store import FolderSyncRecord
from imbue.minds.desktop_client.folder_sync_store import FolderSyncStore

_AGENT_ID = "agent-000102030405060708090a0b0c0d0e0f"
_OTHER_AGENT_ID = "agent-0f0e0d0c0b0a090807060504030201ff"
_DEVICE_ID = "host-0f0e0d0c0b0a09080706050403020100"


def _record(
    local_path: str,
    agent_id: str = _AGENT_ID,
    direction: FolderSyncDirection = FolderSyncDirection.BOTH,
    conflict: FolderSyncConflict = FolderSyncConflict.NEWER,
) -> FolderSyncRecord:
    return FolderSyncRecord(
        agent_id=agent_id,
        local_path=local_path,
        direction=direction,
        conflict=conflict,
        device_id=_DEVICE_ID,
    )


def test_a_remembered_sync_is_there_for_the_next_launch(tmp_path: Path) -> None:
    """The whole point: a store built fresh over the same directory sees it."""
    FolderSyncStore(records_dir=tmp_path).remember(_record("/home/me/notes"))
    reopened = FolderSyncStore(records_dir=tmp_path)
    assert [record.local_path for record in reopened.list_all()] == ["/home/me/notes"]


def test_remembering_a_folder_twice_keeps_the_newer_settings(tmp_path: Path) -> None:
    """Changing direction is not a second sync of the same folder."""
    store = FolderSyncStore(records_dir=tmp_path)
    store.remember(_record("/home/me/notes", direction=FolderSyncDirection.BOTH))
    store.remember(_record("/home/me/notes", direction=FolderSyncDirection.TO_WORKSPACE))
    records = store.list_for_agent(_AGENT_ID)
    assert len(records) == 1
    assert records[0].direction == FolderSyncDirection.TO_WORKSPACE


def test_forgetting_one_folder_leaves_the_others(tmp_path: Path) -> None:
    store = FolderSyncStore(records_dir=tmp_path)
    store.remember(_record("/home/me/notes"))
    store.remember(_record("/home/me/photos"))
    assert store.forget(_AGENT_ID, "/home/me/notes") is True
    assert [record.local_path for record in store.list_for_agent(_AGENT_ID)] == ["/home/me/photos"]


def test_forgetting_something_never_remembered_is_not_an_error(tmp_path: Path) -> None:
    """Turning sync off for a path that was never on is the state asked for."""
    store = FolderSyncStore(records_dir=tmp_path)
    assert store.forget(_AGENT_ID, "/home/me/notes") is False


def test_the_last_folder_leaving_takes_the_file_with_it(tmp_path: Path) -> None:
    """An empty file and an absent one mean the same thing; only one accumulates."""
    store = FolderSyncStore(records_dir=tmp_path)
    store.remember(_record("/home/me/notes"))
    assert (tmp_path / f"{_AGENT_ID}.json").is_file()
    store.forget(_AGENT_ID, "/home/me/notes")
    assert not (tmp_path / f"{_AGENT_ID}.json").exists()


def test_workspaces_are_kept_in_separate_files(tmp_path: Path) -> None:
    store = FolderSyncStore(records_dir=tmp_path)
    store.remember(_record("/home/me/notes"))
    store.remember(_record("/home/me/notes", agent_id=_OTHER_AGENT_ID))
    assert (tmp_path / f"{_AGENT_ID}.json").is_file()
    assert (tmp_path / f"{_OTHER_AGENT_ID}.json").is_file()
    store.forget_workspace(_AGENT_ID)
    assert [record.agent_id for record in store.list_all()] == [_OTHER_AGENT_ID]


def test_a_store_over_a_directory_that_is_not_there_yet_reads_as_empty(tmp_path: Path) -> None:
    """Built at startup, before anything has ever been synced."""
    assert FolderSyncStore(records_dir=tmp_path / "not-yet").list_all() == ()


def test_an_unreadable_record_is_skipped_rather_than_failing_the_rest(tmp_path: Path) -> None:
    """One corrupt file must not cost the user every other sync they had."""
    FolderSyncStore(records_dir=tmp_path).remember(_record("/home/me/notes", agent_id=_OTHER_AGENT_ID))
    (tmp_path / f"{_AGENT_ID}.json").write_text("{ not json")
    assert [record.agent_id for record in FolderSyncStore(records_dir=tmp_path).list_all()] == [_OTHER_AGENT_ID]


def test_a_file_whose_name_is_not_a_workspace_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "notes.json").write_text("{}")
    assert FolderSyncStore(records_dir=tmp_path).list_all() == ()


def test_a_record_from_a_newer_build_still_loads(tmp_path: Path) -> None:
    """A downgrade must not lose the syncs the newer build was running."""
    (tmp_path / f"{_AGENT_ID}.json").write_text(
        '{"records": [{"agent_id": "%s", "local_path": "/home/me/notes", "direction": "BOTH", '
        '"conflict": "NEWER", "device_id": "%s", "something_new": 1}], "also_new": true}' % (_AGENT_ID, _DEVICE_ID)
    )
    records = FolderSyncStore(records_dir=tmp_path).list_all()
    assert [record.local_path for record in records] == ["/home/me/notes"]


def test_a_change_tells_whoever_is_watching(tmp_path: Path) -> None:
    store = FolderSyncStore(records_dir=tmp_path)
    changes: list[int] = []
    store.add_on_change_callback(lambda: changes.append(1))
    store.remember(_record("/home/me/notes"))
    store.forget(_AGENT_ID, "/home/me/notes")
    assert len(changes) == 2
