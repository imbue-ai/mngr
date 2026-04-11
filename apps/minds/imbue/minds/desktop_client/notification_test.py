import json

from _pytest.capture import CaptureFixture

from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification import NotificationUrgency
from imbue.minds.desktop_client.notification import _dispatch_electron_notification


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


def test_electron_notification_output_contains_required_fields(capsys: CaptureFixture[str]) -> None:
    """Verify _dispatch_electron_notification produces valid JSONL with all fields."""
    request = NotificationRequest(
        message="hello from agent",
        title="Alert",
        urgency=NotificationUrgency.CRITICAL,
    )

    _dispatch_electron_notification(request, "my-agent")

    captured = capsys.readouterr()
    output = captured.out.strip()
    event = json.loads(output)
    assert event["event"] == "notification"
    assert event["message"] == "hello from agent"
    assert event["title"] == "Alert"
    assert event["urgency"] == "CRITICAL"
    assert event["agent_name"] == "my-agent"


def test_electron_notification_omits_title_when_none(capsys: CaptureFixture[str]) -> None:
    request = NotificationRequest(message="no title")

    _dispatch_electron_notification(request, "agent-1")

    captured = capsys.readouterr()
    output = captured.out.strip()
    event = json.loads(output)
    assert event["event"] == "notification"
    assert event["message"] == "no title"
    assert "title" not in event


def test_dispatcher_routes_to_electron_when_configured() -> None:
    dispatcher = NotificationDispatcher(is_electron=True)
    assert dispatcher.is_electron is True


def test_dispatcher_routes_to_tkinter_when_not_electron() -> None:
    dispatcher = NotificationDispatcher(is_electron=False)
    assert dispatcher.is_electron is False


def test_dispatch_electron_via_dispatcher(capsys: CaptureFixture[str]) -> None:
    """Verify the full dispatch path for Electron notifications."""
    dispatcher = NotificationDispatcher(is_electron=True)
    request = NotificationRequest(
        message="dispatched message",
        title="Dispatch Title",
        urgency=NotificationUrgency.LOW,
    )
    dispatcher.dispatch(request, "agent-x")

    captured = capsys.readouterr()
    event = json.loads(captured.out.strip())
    assert event["event"] == "notification"
    assert event["message"] == "dispatched message"
    assert event["agent_name"] == "agent-x"
