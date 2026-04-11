import json
from io import StringIO
from unittest.mock import patch

from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification import NotificationUrgency


def test_notification_urgency_values() -> None:
    assert NotificationUrgency.LOW == "LOW"
    assert NotificationUrgency.NORMAL == "NORMAL"
    assert NotificationUrgency.CRITICAL == "CRITICAL"


def test_notification_request_defaults() -> None:
    request = NotificationRequest(message="hello")
    assert request.message == "hello"
    assert request.title is None
    assert request.urgency == NotificationUrgency.NORMAL


def test_notification_request_with_all_fields() -> None:
    request = NotificationRequest(
        message="test message",
        title="Test Title",
        urgency=NotificationUrgency.CRITICAL,
    )
    assert request.message == "test message"
    assert request.title == "Test Title"
    assert request.urgency == NotificationUrgency.CRITICAL


def test_dispatch_electron_writes_jsonl_to_stdout() -> None:
    dispatcher = NotificationDispatcher(is_electron=True)
    request = NotificationRequest(
        message="hello from agent",
        title="Alert",
        urgency=NotificationUrgency.CRITICAL,
    )

    captured = StringIO()
    with patch("imbue.minds.utils.output.sys.stdout", captured):
        dispatcher.dispatch(request, "my-agent")

    output = captured.getvalue().strip()
    event = json.loads(output)
    assert event["event"] == "notification"
    assert event["message"] == "hello from agent"
    assert event["title"] == "Alert"
    assert event["urgency"] == "CRITICAL"
    assert event["agent_name"] == "my-agent"


def test_dispatch_electron_without_title() -> None:
    dispatcher = NotificationDispatcher(is_electron=True)
    request = NotificationRequest(message="no title")

    captured = StringIO()
    with patch("imbue.minds.utils.output.sys.stdout", captured):
        dispatcher.dispatch(request, "agent-1")

    output = captured.getvalue().strip()
    event = json.loads(output)
    assert event["event"] == "notification"
    assert event["message"] == "no title"
    assert "title" not in event


def test_dispatch_tkinter_spawns_thread() -> None:
    """Verify tkinter dispatch starts a background thread without crashing."""
    import tkinter as tk

    dispatcher = NotificationDispatcher(is_electron=False)
    request = NotificationRequest(
        message="test notification",
        urgency=NotificationUrgency.LOW,
    )

    # Patch tkinter.Tk to avoid actually creating a window in CI
    with patch("imbue.minds.desktop_client.notification.tk.Tk") as mock_tk:
        mock_tk.side_effect = tk.TclError("no display")
        # This should not raise -- the tkinter error is caught internally
        dispatcher.dispatch(request, "test-agent")
