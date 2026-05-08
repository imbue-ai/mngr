from pathlib import Path

from watchdog.events import FileCreatedEvent
from watchdog.events import FileDeletedEvent
from watchdog.events import FileModifiedEvent

from imbue.minds_workspace_server.activity_watcher import AgentMarkerWatcher
from imbue.minds_workspace_server.activity_watcher import PERMISSIONS_WAITING_MARKER_FILENAME
from imbue.minds_workspace_server.activity_watcher import _make_marker_file_handler


def test_handler_fires_on_permissions_waiting_marker_create(tmp_path: Path) -> None:
    """Creating ``permissions_waiting`` should trigger the on_change callback.

    Uses ``handler.dispatch`` directly to avoid relying on the watchdog
    observer's filesystem-event timing -- watchdog's macOS FSEvents backend
    can be flaky under heavy parallelism, mirroring the same shortcut taken
    by ``test_applications_file_handler_fires_on_move`` in
    ``agent_manager_test.py``.
    """
    fired: list[bool] = []
    handler = _make_marker_file_handler(lambda: fired.append(True))
    handler.dispatch(FileCreatedEvent(src_path=str(tmp_path / PERMISSIONS_WAITING_MARKER_FILENAME)))
    assert fired == [True]


def test_handler_fires_on_permissions_waiting_marker_delete(tmp_path: Path) -> None:
    fired: list[bool] = []
    handler = _make_marker_file_handler(lambda: fired.append(True))
    handler.dispatch(FileDeletedEvent(src_path=str(tmp_path / PERMISSIONS_WAITING_MARKER_FILENAME)))
    assert fired == [True]


def test_handler_ignores_unrelated_files(tmp_path: Path) -> None:
    fired: list[bool] = []
    handler = _make_marker_file_handler(lambda: fired.append(True))
    handler.dispatch(FileModifiedEvent(src_path=str(tmp_path / "session_started")))
    handler.dispatch(FileModifiedEvent(src_path=str(tmp_path / "claude_session_id")))
    handler.dispatch(FileModifiedEvent(src_path=str(tmp_path / "active")))
    assert fired == []


def test_read_permissions_waiting_reflects_filesystem(tmp_path: Path) -> None:
    """``read_permissions_waiting`` is a pure filesystem read; it does not need
    the watchdog observer to be running, so don't bother starting it --
    watchdog's macOS FSEvents shutdown can stall under parallel xdist."""
    watcher = AgentMarkerWatcher.build("agent-1", tmp_path, lambda _aid: None)

    assert watcher.read_permissions_waiting() is False

    (tmp_path / PERMISSIONS_WAITING_MARKER_FILENAME).touch()
    assert watcher.read_permissions_waiting() is True

    (tmp_path / PERMISSIONS_WAITING_MARKER_FILENAME).unlink()
    assert watcher.read_permissions_waiting() is False


def test_stop_is_safe_when_never_started(tmp_path: Path) -> None:
    """Stopping a watcher that was never started must not raise."""
    watcher = AgentMarkerWatcher.build("agent-1", tmp_path, lambda _aid: None)
    watcher.stop()
    watcher.stop()


# Behavior covered: ``AgentMarkerWatcher.start`` mkdir's the agent state
# directory before scheduling the observer. Verified end-to-end via the
# AgentManager activity-state tests, which create the state dir under
# the host_dir and rely on watching being available there.
