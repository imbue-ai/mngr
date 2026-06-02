import json
import threading
from pathlib import Path
from queue import Queue

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.local_process import RunningProcess
from imbue.mngr.api.observe import get_agent_states_events_path
from imbue.mngr.api.observe import get_default_events_base_dir
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner
from imbue.mngr.utils.polling import wait_for
from imbue.mngr_notifications.config import NotificationsPluginConfig
from imbue.mngr_notifications.errors import MalformedAgentStateEventError
from imbue.mngr_notifications.mock_notifier_test import RecordingNotifier
from imbue.mngr_notifications.watcher import _get_file_size
from imbue.mngr_notifications.watcher import _process_events
from imbue.mngr_notifications.watcher import _read_from_offset
from imbue.mngr_notifications.watcher import watch_for_waiting_agents


def _make_warner() -> MalformedJsonLineWarner:
    return MalformedJsonLineWarner(source_description="test-events")


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


def _run_process_events(
    content: str,
    notifier: RecordingNotifier,
    cg: ConcurrencyGroup,
    was_running: dict[str, bool] | None = None,
    *,
    warner: MalformedJsonLineWarner | None = None,
    plugin_config: NotificationsPluginConfig | None = None,
) -> int:
    """Call _process_events with a fresh warner, returning the bytes consumed."""
    return _process_events(
        content,
        plugin_config if plugin_config is not None else NotificationsPluginConfig(),
        notifier,
        cg,
        warner if warner is not None else _make_warner(),
        {} if was_running is None else was_running,
    )


def test_process_events_running_to_waiting(notification_cg: ConcurrencyGroup) -> None:
    notifier = RecordingNotifier()
    content = _make_state_change_event(agent_name="my-agent", old_state="RUNNING", new_state="WAITING") + "\n"

    consumed = _run_process_events(content, notifier, notification_cg)

    assert consumed == len(content.encode("utf-8"))
    assert len(notifier.calls) == 1
    assert notifier.calls[0][0] == "Agent waiting"
    assert "my-agent" in notifier.calls[0][1]


def test_process_events_waiting_to_running_ignored(notification_cg: ConcurrencyGroup) -> None:
    notifier = RecordingNotifier()
    content = _make_state_change_event(old_state="WAITING", new_state="RUNNING") + "\n"

    _run_process_events(content, notifier, notification_cg)

    assert len(notifier.calls) == 0


def test_process_events_non_state_change_ignored(notification_cg: ConcurrencyGroup) -> None:
    notifier = RecordingNotifier()
    content = (
        json.dumps(
            {"type": "AGENT_STATE", "timestamp": "2026-01-01T00:00:00Z", "event_id": "evt-x", "source": "mngr/agents"}
        )
        + "\n"
    )

    _run_process_events(content, notifier, notification_cg)

    assert len(notifier.calls) == 0


def test_process_events_partial_trailing_line_is_held_back(notification_cg: ConcurrencyGroup) -> None:
    """A torn trailing line is not consumed: only complete-line bytes are returned and it does not crash."""
    notifier = RecordingNotifier()
    complete = _make_state_change_event(agent_name="done-agent", old_state="RUNNING", new_state="WAITING") + "\n"
    # A torn write at the tail: no trailing newline, so it must not be consumed.
    partial = '{"type": "AGENT_STATE_CHANGE", "old_state": "RUNN'
    content = complete + partial

    consumed = _run_process_events(content, notifier, notification_cg)

    # Only the complete line's bytes are consumed; the partial is left for the next read.
    assert consumed == len(complete.encode("utf-8"))
    assert len(notifier.calls) == 1
    assert "done-agent" in notifier.calls[0][1]


def test_process_events_partial_line_completed_on_next_read(notification_cg: ConcurrencyGroup) -> None:
    """A line torn across two reads is reconstructed and fires once the writer flushes the rest."""
    notifier = RecordingNotifier()
    warner = _make_warner()
    full_line = _make_state_change_event(agent_name="split-agent", old_state="RUNNING", new_state="WAITING") + "\n"
    first_chunk = full_line[: len(full_line) // 2]

    consumed_first = _run_process_events(first_chunk, notifier, notification_cg, warner=warner)
    assert consumed_first == 0
    assert len(notifier.calls) == 0

    # The offset advanced by 0, so the next read re-presents the full line and it completes.
    consumed_second = _run_process_events(full_line, notifier, notification_cg, warner=warner)
    assert consumed_second == len(full_line.encode("utf-8"))
    assert len(notifier.calls) == 1
    assert "split-agent" in notifier.calls[0][1]


def test_process_events_mid_file_corruption_warns_and_skips(notification_cg: ConcurrencyGroup) -> None:
    """A genuinely corrupt mid-file line is skipped (not crashed on) and following events still process."""
    notifier = RecordingNotifier()
    content = (
        "this is not valid json\n"
        + _make_state_change_event(agent_name="after-corruption", old_state="RUNNING", new_state="WAITING")
        + "\n"
    )

    consumed = _run_process_events(content, notifier, notification_cg)

    assert consumed == len(content.encode("utf-8"))
    assert len(notifier.calls) == 1
    assert "after-corruption" in notifier.calls[0][1]


def test_process_events_missing_agent_id_raises(notification_cg: ConcurrencyGroup) -> None:
    """A state-change record missing agent_id fails loudly instead of bucketing under 'unknown'."""
    notifier = RecordingNotifier()
    content = (
        json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "AGENT_STATE_CHANGE",
                "event_id": "evt-x",
                "source": "mngr/agent_states",
                "agent_name": "named",
                "old_state": "RUNNING",
                "new_state": "UNKNOWN",
            }
        )
        + "\n"
    )

    with pytest.raises(MalformedAgentStateEventError):
        _run_process_events(content, notifier, notification_cg)

    assert len(notifier.calls) == 0


def test_process_events_missing_agent_name_raises(notification_cg: ConcurrencyGroup) -> None:
    """A firing state-change record missing agent_name fails loudly instead of saying 'unknown is waiting'."""
    notifier = RecordingNotifier()
    content = (
        json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "AGENT_STATE_CHANGE",
                "event_id": "evt-x",
                "source": "mngr/agent_states",
                "agent_id": "agent-x",
                "old_state": "RUNNING",
                "new_state": "WAITING",
            }
        )
        + "\n"
    )

    with pytest.raises(MalformedAgentStateEventError):
        _run_process_events(content, notifier, notification_cg)

    assert len(notifier.calls) == 0


def test_process_events_multiple_lines(notification_cg: ConcurrencyGroup) -> None:
    notifier = RecordingNotifier()
    lines = (
        "\n".join(
            [
                _make_state_change_event(agent_name="agent-a", old_state="RUNNING", new_state="WAITING"),
                _make_state_change_event(agent_name="agent-b", old_state="WAITING", new_state="RUNNING"),
                _make_state_change_event(agent_name="agent-c", old_state="RUNNING", new_state="WAITING"),
            ]
        )
        + "\n"
    )

    _run_process_events(lines, notifier, notification_cg)

    assert len(notifier.calls) == 2
    assert "agent-a" in notifier.calls[0][1]
    assert "agent-c" in notifier.calls[1][1]


def test_process_events_running_unknown_waiting_fires_notification(notification_cg: ConcurrencyGroup) -> None:
    """The indirect RUNNING -> UNKNOWN -> WAITING sequence fires the notification on the UNKNOWN -> WAITING step."""
    notifier = RecordingNotifier()
    was_running: dict[str, bool] = {}
    lines = (
        "\n".join(
            [
                _make_state_change_event(
                    agent_id="agent-1", agent_name="indirect", old_state="RUNNING", new_state="UNKNOWN"
                ),
                _make_state_change_event(
                    agent_id="agent-1", agent_name="indirect", old_state="UNKNOWN", new_state="WAITING"
                ),
            ]
        )
        + "\n"
    )

    _run_process_events(lines, notifier, notification_cg, was_running)

    assert len(notifier.calls) == 1
    assert "indirect" in notifier.calls[0][1]
    # Bit is consumed by the indirect transition firing
    assert was_running == {}


def test_process_events_unknown_running_clears_was_running_bit(notification_cg: ConcurrencyGroup) -> None:
    """RUNNING -> UNKNOWN -> RUNNING clears the bit so a later UNKNOWN -> WAITING does not fire."""
    notifier = RecordingNotifier()
    was_running: dict[str, bool] = {}
    lines = (
        "\n".join(
            [
                _make_state_change_event(
                    agent_id="agent-2", agent_name="recovered", old_state="RUNNING", new_state="UNKNOWN"
                ),
                _make_state_change_event(
                    agent_id="agent-2", agent_name="recovered", old_state="UNKNOWN", new_state="RUNNING"
                ),
                _make_state_change_event(
                    agent_id="agent-2", agent_name="recovered", old_state="UNKNOWN", new_state="WAITING"
                ),
            ]
        )
        + "\n"
    )

    _run_process_events(lines, notifier, notification_cg, was_running)

    # Bit was cleared by UNKNOWN -> RUNNING; the trailing UNKNOWN -> WAITING does not fire.
    assert len(notifier.calls) == 0


def test_process_events_unknown_to_waiting_without_prior_running_does_not_fire(
    notification_cg: ConcurrencyGroup,
) -> None:
    """An UNKNOWN -> WAITING transition with no remembered RUNNING-before-UNKNOWN bit does not fire."""
    notifier = RecordingNotifier()
    content = _make_state_change_event(agent_id="agent-3", old_state="UNKNOWN", new_state="WAITING") + "\n"

    _run_process_events(content, notifier, notification_cg)

    assert len(notifier.calls) == 0


def test_process_events_running_to_unknown_does_not_fire(notification_cg: ConcurrencyGroup) -> None:
    """RUNNING -> UNKNOWN alone does not fire a notification (only sets the per-agent bit)."""
    notifier = RecordingNotifier()
    was_running: dict[str, bool] = {}
    content = _make_state_change_event(agent_id="agent-4", old_state="RUNNING", new_state="UNKNOWN") + "\n"

    _run_process_events(content, notifier, notification_cg, was_running)

    assert len(notifier.calls) == 0
    assert was_running == {"agent-4": True}


def test_process_events_was_running_bit_is_per_agent(notification_cg: ConcurrencyGroup) -> None:
    """The was-running-before-UNKNOWN bit must be tracked per agent_id, not globally."""
    notifier = RecordingNotifier()
    was_running: dict[str, bool] = {}
    lines = (
        "\n".join(
            [
                # agent-A: RUNNING -> UNKNOWN (sets bit for A)
                _make_state_change_event(
                    agent_id="agent-A", agent_name="aaa", old_state="RUNNING", new_state="UNKNOWN"
                ),
                # agent-B: UNKNOWN -> WAITING (bit for B is NOT set; should not fire)
                _make_state_change_event(
                    agent_id="agent-B", agent_name="bbb", old_state="UNKNOWN", new_state="WAITING"
                ),
                # agent-A: UNKNOWN -> WAITING (bit IS set; should fire)
                _make_state_change_event(
                    agent_id="agent-A", agent_name="aaa", old_state="UNKNOWN", new_state="WAITING"
                ),
            ]
        )
        + "\n"
    )

    _run_process_events(lines, notifier, notification_cg, was_running)

    assert len(notifier.calls) == 1
    assert "aaa" in notifier.calls[0][1]


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


@pytest.mark.timeout(30)
def test_watch_processes_events_then_stops(temp_mngr_ctx: MngrContext) -> None:
    """Watcher reads new events when file grows and stops when stop_event is set."""
    events_path = get_agent_states_events_path(get_default_events_base_dir(temp_mngr_ctx.config))
    events_path.parent.mkdir(parents=True, exist_ok=True)

    notifier = RecordingNotifier()
    stop_event = threading.Event()
    ready_event = threading.Event()

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
            "ready_event": ready_event,
        },
    )
    watcher_thread.start()

    try:
        # Wait for the watcher to capture its initial file offset before writing
        assert ready_event.wait(timeout=5), "Watcher did not become ready"

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
