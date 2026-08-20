"""Unit tests for the remote-state watchdog handler in :mod:`imbue.mngr_latchkey.discovery`.

Drives :class:`_LatchkeyStateChangeHandler.dispatch` directly with
synthetic watchdog events, the same way the observer's emitter thread
would, so the event-type allowlist and path matching are covered without
spawning a real observer or touching inotify.

The critical regression covered here: watchdog's Linux (inotify) observer
dispatches read-lifecycle events (``FileOpenedEvent`` / ``FileClosedNoWriteEvent``)
for every *read* of a watched file, and the sync callbacks themselves read
the watched files -- so a handler that reacted to those events re-triggered
itself forever (a full VPS re-sync every ~6s for the supervisor's lifetime).
"""

from pathlib import Path

from watchdog.events import DirModifiedEvent
from watchdog.events import FileClosedEvent
from watchdog.events import FileClosedNoWriteEvent
from watchdog.events import FileCreatedEvent
from watchdog.events import FileDeletedEvent
from watchdog.events import FileModifiedEvent
from watchdog.events import FileMovedEvent
from watchdog.events import FileOpenedEvent

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.discovery import _LatchkeyStateChangeHandler
from imbue.mngr_latchkey.store import permissions_path_for_host


class _SyncRecorder(MutableModel):
    """Records the callback invocations the handler under test fires."""

    credential_sync_count: int = 0
    permission_sync_host_ids: list[str] = []

    def on_credentials_changed(self) -> None:
        self.credential_sync_count += 1

    def on_host_permissions_changed(self, host_id_str: str) -> None:
        self.permission_sync_host_ids.append(host_id_str)


def _build_handler(tmp_path: Path, host_id: HostId, recorder: _SyncRecorder) -> _LatchkeyStateChangeHandler:
    return _LatchkeyStateChangeHandler(
        credentials_path=tmp_path / "credentials.json.enc",
        plugin_data_dir=tmp_path / "mngr_latchkey",
        known_remote_host_ids=lambda: frozenset({str(host_id)}),
        on_credentials_changed=recorder.on_credentials_changed,
        on_host_permissions_changed=recorder.on_host_permissions_changed,
    )


def test_read_lifecycle_events_do_not_trigger_any_sync(tmp_path: Path) -> None:
    """A pure read of a watched file (open + close-no-write) must be inert.

    This is the feedback-loop regression: the sync callbacks read the very
    files being watched, so reacting to read events re-triggers the sync
    forever.
    """
    host_id = HostId()
    recorder = _SyncRecorder()
    handler = _build_handler(tmp_path, host_id, recorder)
    credentials_path = str(tmp_path / "credentials.json.enc")
    permissions_path = str(permissions_path_for_host(tmp_path / "mngr_latchkey", host_id))

    for path in (credentials_path, permissions_path):
        handler.dispatch(FileOpenedEvent(path))
        handler.dispatch(FileClosedNoWriteEvent(path))

    assert recorder.credential_sync_count == 0
    assert recorder.permission_sync_host_ids == []


def test_close_after_write_event_does_not_double_fire(tmp_path: Path) -> None:
    """``FileClosedEvent`` (IN_CLOSE_WRITE) is excluded: the accompanying
    ``FileModifiedEvent`` already triggers the sync, so reacting to both
    would double every sync."""
    host_id = HostId()
    recorder = _SyncRecorder()
    handler = _build_handler(tmp_path, host_id, recorder)

    handler.dispatch(FileClosedEvent(str(tmp_path / "credentials.json.enc")))

    assert recorder.credential_sync_count == 0


def test_modified_event_on_credentials_triggers_credential_sync(tmp_path: Path) -> None:
    host_id = HostId()
    recorder = _SyncRecorder()
    handler = _build_handler(tmp_path, host_id, recorder)

    handler.dispatch(FileModifiedEvent(str(tmp_path / "credentials.json.enc")))

    assert recorder.credential_sync_count == 1
    assert recorder.permission_sync_host_ids == []


def test_modified_event_on_known_host_permissions_triggers_permission_sync(tmp_path: Path) -> None:
    host_id = HostId()
    recorder = _SyncRecorder()
    handler = _build_handler(tmp_path, host_id, recorder)
    permissions_path = permissions_path_for_host(tmp_path / "mngr_latchkey", host_id)

    handler.dispatch(FileModifiedEvent(str(permissions_path)))

    assert recorder.permission_sync_host_ids == [str(host_id)]
    assert recorder.credential_sync_count == 0


def test_created_and_deleted_events_trigger_sync(tmp_path: Path) -> None:
    host_id = HostId()
    recorder = _SyncRecorder()
    handler = _build_handler(tmp_path, host_id, recorder)
    credentials_path = str(tmp_path / "credentials.json.enc")

    handler.dispatch(FileCreatedEvent(credentials_path))
    handler.dispatch(FileDeletedEvent(credentials_path))

    assert recorder.credential_sync_count == 2


def test_atomic_write_rename_dest_triggers_sync(tmp_path: Path) -> None:
    """An atomic write (tmp sibling -> rename onto the real file) surfaces the
    real file as the move *dest*; the handler must match it."""
    host_id = HostId()
    recorder = _SyncRecorder()
    handler = _build_handler(tmp_path, host_id, recorder)
    permissions_path = permissions_path_for_host(tmp_path / "mngr_latchkey", host_id)

    handler.dispatch(FileMovedEvent(str(permissions_path.parent / ".tmp.abc123"), str(permissions_path)))

    assert recorder.permission_sync_host_ids == [str(host_id)]


def test_unrelated_paths_and_unknown_hosts_are_ignored(tmp_path: Path) -> None:
    host_id = HostId()
    unknown_host_id = HostId()
    recorder = _SyncRecorder()
    handler = _build_handler(tmp_path, host_id, recorder)
    unknown_permissions_path = permissions_path_for_host(tmp_path / "mngr_latchkey", unknown_host_id)

    handler.dispatch(FileModifiedEvent(str(tmp_path / "gateway.log")))
    handler.dispatch(FileModifiedEvent(str(unknown_permissions_path)))

    assert recorder.credential_sync_count == 0
    assert recorder.permission_sync_host_ids == []


def test_directory_events_are_ignored(tmp_path: Path) -> None:
    host_id = HostId()
    recorder = _SyncRecorder()
    handler = _build_handler(tmp_path, host_id, recorder)

    handler.dispatch(DirModifiedEvent(str(tmp_path)))

    assert recorder.credential_sync_count == 0
    assert recorder.permission_sync_host_ids == []
