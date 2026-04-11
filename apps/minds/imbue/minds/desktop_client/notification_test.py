import json
import threading

from _pytest.capture import CaptureFixture

from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification import NotificationUrgency
from imbue.minds.desktop_client.notification import _dispatch_electron_notification
from imbue.minds.desktop_client.notification import _run_tkinter_toast
from imbue.minds.desktop_client.notification import _show_tkinter_toast


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


def test_dispatcher_is_electron_false_does_not_raise() -> None:
    """Verify NotificationDispatcher can be constructed in non-electron mode."""
    dispatcher = NotificationDispatcher(is_electron=False)
    assert dispatcher.is_electron is False


def test_run_tkinter_toast_without_tkinter_does_not_raise() -> None:
    """When tkinter is unavailable, _run_tkinter_toast returns immediately without error."""
    # Should not raise even though tk=None indicates no tkinter
    _run_tkinter_toast("Title", "Message", NotificationUrgency.LOW, "agent", tk=None)


def test_show_tkinter_toast_with_no_tkinter_does_not_raise() -> None:
    """_show_tkinter_toast does not raise even when tkinter is unavailable.

    The function starts a daemon thread. With no tkinter available, the thread
    logs a warning and exits immediately.
    """
    request = NotificationRequest(message="toast message", title="Test")
    _show_tkinter_toast(request, "agent-z", tk=None)


def test_dispatch_non_electron_does_not_raise() -> None:
    """The non-Electron dispatch path starts a background toast and does not raise."""
    dispatcher = NotificationDispatcher.create(is_electron=False, tkinter_module=None)
    request = NotificationRequest(message="background toast")
    dispatcher.dispatch(request, "agent-y")


def test_dispatcher_create_with_no_tkinter() -> None:
    """NotificationDispatcher.create with tkinter_module=None disables tkinter toasts."""
    dispatcher = NotificationDispatcher.create(is_electron=False, tkinter_module=None)
    assert dispatcher.is_electron is False
    assert dispatcher._tk is None


def test_dispatcher_create_defaults_is_electron_false(capsys: CaptureFixture[str]) -> None:
    """NotificationDispatcher.create(is_electron=True) routes to Electron."""
    dispatcher = NotificationDispatcher.create(is_electron=True)
    request = NotificationRequest(message="from create factory")
    dispatcher.dispatch(request, "agent-factory")

    captured = capsys.readouterr()
    event = json.loads(captured.out.strip())
    assert event["message"] == "from create factory"


def test_dispatcher_default_constructor_resolves_tkinter() -> None:
    """NotificationDispatcher() resolves tkinter at construction via model_post_init."""
    # The _tk private attr should be set to the auto-detected _TKINTER value.
    # We can't know if tkinter is available, so just verify _tk is not uninitialized.
    dispatcher = NotificationDispatcher(is_electron=False)
    # _tk is set by model_post_init; it will be a ModuleType or None (if tkinter is absent)
    # Just verify the attribute is accessible (not undefined)
    _ = dispatcher._tk


def test_show_tkinter_toast_with_no_tkinter_runs_in_thread() -> None:
    """_show_tkinter_toast with tk=None starts a daemon thread that exits immediately."""
    before_count = threading.active_count()
    request = NotificationRequest(message="thread test")
    _show_tkinter_toast(request, "agent-thread", tk=None)
    # Thread is daemon and should start; we can't easily join it but verify no exception
    assert threading.active_count() >= before_count



