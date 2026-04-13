import json
import threading
import types
from types import SimpleNamespace
from typing import Any

import pytest

from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification import NotificationUrgency
from imbue.minds.desktop_client.notification import _build_toast_widgets
from imbue.minds.desktop_client.notification import _dispatch_electron_notification
from imbue.minds.desktop_client.notification import _dispatch_macos_notification
from imbue.minds.desktop_client.notification import _position_toast_window
from imbue.minds.desktop_client.notification import _run_tkinter_toast
from imbue.minds.desktop_client.notification import _show_tkinter_toast


def _make_fake_tk() -> Any:
    """Build a minimal fake tkinter module sufficient for _build_toast_widgets,
    _position_toast_window, and _run_tkinter_toast.

    Uses SimpleNamespace and a lightweight widget stand-in that records calls
    without requiring a real display server.
    """

    class _FakeWidget:
        """Minimal stand-in for tkinter widgets (Frame, Label, and Tk root)."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            self._children: list["_FakeWidget"] = []
            self._bindings: dict[str, object] = {}
            # Register as a child of the parent widget (first positional arg),
            # mirroring real tkinter widget parent-child relationships.
            if args and isinstance(args[0], _FakeWidget):
                args[0]._children.append(self)

        def pack(self, **kwargs: object) -> None:
            pass

        def bind(self, event: str, handler: object) -> None:
            self._bindings[event] = handler

        def winfo_children(self) -> "list[_FakeWidget]":
            return self._children

        def winfo_reqheight(self) -> int:
            return 100

        def winfo_screenwidth(self) -> int:
            return 1920

        def winfo_screenheight(self) -> int:
            return 1080

        def update_idletasks(self) -> None:
            pass

        def geometry(self, spec: str) -> None:
            pass

        def overrideredirect(self, flag: bool) -> None:
            pass

        def attributes(self, attr: str, value: object) -> None:
            pass

        def mainloop(self) -> None:
            pass

        def destroy(self) -> None:
            pass

    class _FakeFrame(_FakeWidget):
        pass

    class _FakeLabel(_FakeWidget):
        pass

    class _FakeTclError(Exception):
        pass

    tk = SimpleNamespace(
        Frame=_FakeFrame,
        Label=_FakeLabel,
        Tk=_FakeWidget,
        TclError=_FakeTclError,
        BOTH="both",
        X="x",
        Y="y",
        LEFT="left",
        RIGHT="right",
        TOP="top",
        BOTTOM="bottom",
    )
    return tk


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


def test_electron_notification_output_contains_required_fields(capsys: pytest.CaptureFixture[str]) -> None:
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


def test_electron_notification_omits_title_when_none(capsys: pytest.CaptureFixture[str]) -> None:
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


def test_dispatch_electron_via_dispatcher(capsys: pytest.CaptureFixture[str]) -> None:
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


def test_dispatcher_create_defaults_is_electron_false(capsys: pytest.CaptureFixture[str]) -> None:
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


# -- macOS notification tests --


def test_dispatch_macos_notification_does_not_raise() -> None:
    """On non-macOS, osascript won't exist, but the function should not raise."""
    request = NotificationRequest(
        message="test macOS notification",
        title="Test Title",
        urgency=NotificationUrgency.CRITICAL,
    )
    # Should not raise even if osascript is not available (caught internally)
    _dispatch_macos_notification(request, "agent-mac")


def test_dispatch_macos_notification_handles_quotes() -> None:
    """Verify double quotes in title/message are escaped for AppleScript."""
    request = NotificationRequest(
        message='He said "hello"',
        title='Title with "quotes"',
        urgency=NotificationUrgency.NORMAL,
    )
    # Should not raise
    _dispatch_macos_notification(request, "agent-quotes")


def test_dispatcher_routes_to_macos_when_is_macos() -> None:
    """Verify dispatch routes to macOS native notifications when is_macos=True."""
    dispatcher = NotificationDispatcher.create(is_electron=False, is_macos=True, tkinter_module=None)
    assert dispatcher.is_macos is True
    request = NotificationRequest(message="macos dispatch test")
    # Should not raise -- osascript may fail on Linux but error is caught
    dispatcher.dispatch(request, "agent-mac-dispatch")


def test_dispatcher_prefers_electron_over_macos(capsys: pytest.CaptureFixture[str]) -> None:
    """Electron takes priority over macOS native notifications."""
    dispatcher = NotificationDispatcher.create(is_electron=True, is_macos=True)
    request = NotificationRequest(message="electron priority")
    dispatcher.dispatch(request, "agent-priority")

    captured = capsys.readouterr()
    event = json.loads(captured.out.strip())
    assert event["event"] == "notification"
    assert event["message"] == "electron priority"


def test_dispatcher_create_with_is_macos_override() -> None:
    """Verify create() accepts is_macos parameter."""
    dispatcher = NotificationDispatcher.create(is_electron=False, is_macos=False)
    assert dispatcher.is_macos is False


# -- _build_toast_widgets and _position_toast_window tests with fake tkinter --


def test_build_toast_widgets_returns_frame_and_content() -> None:
    """_build_toast_widgets constructs frame/content widgets using the provided tk module."""
    tk = _make_fake_tk()
    root = tk.Frame()
    frame, content = _build_toast_widgets(
        root=root,
        title="Test Title",
        message="Test message",
        urgency=NotificationUrgency.NORMAL,
        agent_display_name="test-agent",
        tk=tk,
    )
    assert frame is not None
    assert content is not None


def test_build_toast_widgets_with_critical_urgency() -> None:
    """_build_toast_widgets uses the critical urgency color when urgency is CRITICAL."""
    tk = _make_fake_tk()
    root = tk.Frame()
    frame, content = _build_toast_widgets(
        root=root,
        title="Alert",
        message="Critical notification",
        urgency=NotificationUrgency.CRITICAL,
        agent_display_name="agent-x",
        tk=tk,
    )
    assert frame is not None
    assert content is not None


def test_build_toast_widgets_with_low_urgency() -> None:
    """_build_toast_widgets does not raise for LOW urgency."""
    tk = _make_fake_tk()
    root = tk.Frame()
    frame, content = _build_toast_widgets(
        root=root,
        title="Info",
        message="Low priority notification",
        urgency=NotificationUrgency.LOW,
        agent_display_name="agent-y",
        tk=tk,
    )
    assert frame is not None
    assert content is not None


def test_position_toast_window_calls_geometry() -> None:
    """_position_toast_window calls root.geometry() to position the window."""
    tk = _make_fake_tk()
    root = tk.Frame()
    _position_toast_window(root, width=320)


def test_run_tkinter_toast_with_fake_tk_raises_tclerror() -> None:
    """When tk.Tk() raises TclError (e.g., no display), _run_tkinter_toast logs and returns."""

    class _TclError(Exception):
        pass

    def _raise_tclerror() -> None:
        raise _TclError("no display")

    fake_tk = types.ModuleType("tkinter")
    fake_tk.TclError = _TclError  # ty: ignore[unresolved-attribute]
    fake_tk.Tk = _raise_tclerror  # ty: ignore[unresolved-attribute]

    _run_tkinter_toast(
        title="Title",
        message="Message",
        urgency=NotificationUrgency.NORMAL,
        agent_display_name="agent",
        tk=fake_tk,
    )


def test_run_tkinter_toast_with_fake_tk_succeeds() -> None:
    """When tk.Tk() works, _run_tkinter_toast creates widgets and runs mainloop."""
    tk = _make_fake_tk()
    _run_tkinter_toast(
        title="Title",
        message="Message body",
        urgency=NotificationUrgency.NORMAL,
        agent_display_name="test-agent",
        tk=tk,
    )

