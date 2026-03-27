import json
from collections.abc import Generator
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_notifications.config import NotificationsPluginConfig
from imbue.mngr_notifications.mock_notifier_test import RecordingNotifier
from imbue.mngr_notifications.watcher import _get_file_size
from imbue.mngr_notifications.watcher import _process_events
from imbue.mngr_notifications.watcher import _read_from_offset


@pytest.fixture()
def notification_cg() -> Generator[ConcurrencyGroup, None, None]:
    with ConcurrencyGroup(name="test-notification") as group:
        yield group


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
