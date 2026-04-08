import json
import threading
from pathlib import Path
from queue import Queue

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.local_process import RunningProcess
from imbue.mngr.api.observe import get_agent_states_events_path
from imbue.mngr.api.observe import get_default_events_base_dir
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.utils.polling import wait_for
from imbue.mngr_notifications.config import NotificationsPluginConfig
from imbue.mngr_notifications.mock_notifier_test import RecordingNotifier
from imbue.mngr_notifications.watcher import _get_file_size
from imbue.mngr_notifications.watcher import _process_events
from imbue.mngr_notifications.watcher import _read_from_offset
from imbue.mngr_notifications.watcher import watch_for_waiting_agents


class _FakeDeadProcess(RunningProcess):
    """Simulates a RunningProcess that has already exited."""

    def __init__(self, exit_code: int, stderr: str = "") -> None:
        super().__init__(command=["fake"], output_queue=Queue(), shutdown_event=threading.Event())
        self._exit_code = exit_code
        self._fake_stderr = stderr

    @property
    def returncode(self) -> int:
        return self._exit_code

    def read_stderr(self) -> str:
        return self._fake_stderr


def _make_state_change_event(
    agent_name: str = "test-agent",
    agent_id: str = "agent-123",
    old_state: str = "RUNNING",
    new_state: str = "WAITING",
) -> str:
    return json.dumps(
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "AGENT_STATE_CHANGE",
            "event_id": "evt-abc123",
            "source": "mngr/agent_states",
            "agent_id": agent_id,
            "agent_name": agent_name,
            "old_state": old_state,
            "new_state": new_state,
            "agent": {},
        }
    )


def test_get_file_size_existing(tmp_path: Path) -> None:
    f = tmp_path / "test.jsonl"
    f.write_text("hello\n")
    assert _get_file_size(f) == 6


def test_get_file_size_nonexistent(tmp_path: Path) -> None:
    assert _get_file_size(tmp_path / "nonexistent") == 0


def test_read_from_offset(tmp_path: Path) -> None:
    f = tmp_path / "test.jsonl"
    f.write_text("line1\nline2\n")
    assert _read_from_offset(f, 6) == "line2\n"


def test_read_from_offset_start(tmp_path: Path) -> None:
    f = tmp_path / "test.jsonl"
    f.write_text("all content\n")
    assert _read_from_offset(f, 0) == "all content\n"


def test_read_from_offset_nonexistent(tmp_path: Path) -> None:
    assert _read_from_offset(tmp_path / "nonexistent", 0) == ""


def test_process_events_running_to_waiting(notification_cg: ConcurrencyGroup) -> None:
    notifier = RecordingNotifier()
    content = _make_state_change_event(agent_name="my-agent", old_state="RUNNING", new_state="WAITING")

    _process_events(content, NotificationsPluginConfig(), notifier, notification_cg)

    assert len(notifier.calls) == 1
    assert notifier.calls[0][0] == "Agent waiting"
    assert "my-agent" in notifier.calls[0][1]


def test_process_events_waiting_to_running_ignored(notification_cg: ConcurrencyGroup) -> None:
    notifier = RecordingNotifier()
    content = _make_state_change_event(old_state="WAITING", new_state="RUNNING")

    _process_events(content, NotificationsPluginConfig(), notifier, notification_cg)

    assert len(notifier.calls) == 0


def test_process_events_non_state_change_ignored(notification_cg: ConcurrencyGroup) -> None:
    notifier = RecordingNotifier()
    content = json.dumps(
        {"type": "AGENT_STATE", "timestamp": "2026-01-01T00:00:00Z", "event_id": "evt-x", "source": "mngr/agents"}
    )

    _process_events(content, NotificationsPluginConfig(), notifier, notification_cg)

    assert len(notifier.calls) == 0


def test_process_events_malformed_json_ignored(notification_cg: ConcurrencyGroup) -> None:
    notifier = RecordingNotifier()

    _process_events("not valid json\n", NotificationsPluginConfig(), notifier, notification_cg)

    assert len(notifier.calls) == 0


def test_process_events_multiple_lines(notification_cg: ConcurrencyGroup) -> None:
    notifier = RecordingNotifier()
    lines = "\n".join(
        [
            _make_state_change_event(agent_name="agent-a", old_state="RUNNING", new_state="WAITING"),
            _make_state_change_event(agent_name="agent-b", old_state="WAITING", new_state="RUNNING"),
            _make_state_change_event(agent_name="agent-c", old_state="RUNNING", new_state="WAITING"),
        ]
    )

    _process_events(lines, NotificationsPluginConfig(), notifier, notification_cg)

    assert len(notifier.calls) == 2
    assert "agent-a" in notifier.calls[0][1]
    assert "agent-c" in notifier.calls[1][1]


# --- watch_for_waiting_agents ---


def test_watch_exits_when_observe_process_dies(temp_mngr_ctx: MngrContext) -> None:
    """Watcher exits early when the observe process has a non-None returncode."""
    events_path = get_agent_states_events_path(get_default_events_base_dir(temp_mngr_ctx.config))
    events_path.parent.mkdir(parents=True, exist_ok=True)

    notifier = RecordingNotifier()
    dead_process = _FakeDeadProcess(exit_code=1, stderr="some error")

    # The watcher should detect the dead process on the first iteration and return
    watch_for_waiting_agents(
        mngr_ctx=temp_mngr_ctx,
        plugin_config=NotificationsPluginConfig(),
        notifier=notifier,
        observe_process=dead_process,
    )

    assert len(notifier.calls) == 0


def test_watch_exits_when_observe_process_dies_no_stderr(temp_mngr_ctx: MngrContext) -> None:
    """Watcher exits when observe dies with no stderr output."""
    events_path = get_agent_states_events_path(get_default_events_base_dir(temp_mngr_ctx.config))
    events_path.parent.mkdir(parents=True, exist_ok=True)

    notifier = RecordingNotifier()
    dead_process = _FakeDeadProcess(exit_code=0, stderr="")

    watch_for_waiting_agents(
        mngr_ctx=temp_mngr_ctx,
        plugin_config=NotificationsPluginConfig(),
        notifier=notifier,
        observe_process=dead_process,
    )

    assert len(notifier.calls) == 0


def test_watch_processes_events_then_stops(temp_mngr_ctx: MngrContext) -> None:
    """Watcher reads new events when file grows and stops when stop_event is set."""
    events_path = get_agent_states_events_path(get_default_events_base_dir(temp_mngr_ctx.config))
    events_path.parent.mkdir(parents=True, exist_ok=True)

    notifier = RecordingNotifier()
    stop_event = threading.Event()

    # Write an event before starting
    event = _make_state_change_event(agent_name="pre-agent")
    events_path.write_text(event + "\n")

    watcher_thread = threading.Thread(
        target=watch_for_waiting_agents,
        kwargs={
            "mngr_ctx": temp_mngr_ctx,
            "plugin_config": NotificationsPluginConfig(),
            "notifier": notifier,
            "stop_event": stop_event,
        },
    )
    watcher_thread.start()

    try:
        # Append a new event after the watcher starts
        with events_path.open("a") as f:
            f.write(_make_state_change_event(agent_name="new-agent") + "\n")

        # Wait for the watcher to pick it up
        wait_for(
            lambda: len(notifier.calls) > 0,
            timeout=10,
            poll_interval=0.1,
            error_message="Watcher did not send notification for new event",
        )

        assert len(notifier.calls) >= 1
        assert "new-agent" in notifier.calls[0][1]
    finally:
        stop_event.set()
        watcher_thread.join(timeout=10)
