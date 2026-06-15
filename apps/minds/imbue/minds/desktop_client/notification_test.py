import json
import types
from types import SimpleNamespace
from typing import Any

import pytest

from imbue.minds.desktop_client.notification import DispatchChannel
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification import NotificationUrgency
from imbue.minds.desktop_client.notification import _TKINTER
from imbue.minds.desktop_client.notification import _URGENCY_COLOR_BY_LEVEL
from imbue.minds.desktop_client.notification import _build_osascript_notification
from imbue.minds.desktop_client.notification import _build_toast_widgets
from imbue.minds.desktop_client.notification import _dispatch_electron_notification
from imbue.minds.desktop_client.notification import _position_toast_window
from imbue.minds.desktop_client.notification import _run_tkinter_toast
from imbue.minds.desktop_client.notification import _select_dispatch_channel
from imbue.minds.desktop_client.notification import _show_tkinter_toast


def _make_fake_tk() -> Any:
    """Build a minimal fake tkinter module sufficient for _build_toast_widgets,
    _position_toast_window, and _run_tkinter_toast.

    Uses SimpleNamespace and a lightweight widget stand-in that records calls
    without requiring a real display server.
    """

    class _FakeWidget:
        """Minimal stand-in for tkinter widgets (Frame, Label, and Tk root).

        Records the construction kwargs (e.g. ``text=``, ``bg=``) so tests can
        introspect the widget tree built by ``_build_toast_widgets``, and the
        geometry spec passed to ``geometry`` so tests can assert on positioning.
        """

        def __init__(self, *args: object, **kwargs: object) -> None:
            self._children: list["_FakeWidget"] = []
            self._bindings: dict[str, object] = {}
            self.kwargs: dict[str, object] = dict(kwargs)
            self.geometry_spec: str | None = None
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
            self.geometry_spec = spec

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
    """The non-Electron/non-macOS dispatch path starts a background toast and does not raise.

    is_macos is forced to False so the test exercises the tkinter branch regardless
    of the host platform (and does not fire a real macOS Notification Center banner
    when the suite runs on a developer's Mac).
    """
    dispatcher = NotificationDispatcher.create(is_electron=False, is_macos=False, tkinter_module=None)
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
    """NotificationDispatcher() resolves _tk to the module-level _TKINTER via model_post_init."""
    dispatcher = NotificationDispatcher(is_electron=False)
    # model_post_init sets _tk to the auto-detected module-level _TKINTER value
    # (a ModuleType when tkinter is importable, or None on headless hosts).
    assert dispatcher._tk is _TKINTER


# -- macOS notification tests --


def test_build_osascript_notification_escapes_double_quotes() -> None:
    """Double quotes in title, message, and subtitle must be escaped to \\" so the
    AppleScript string literals are syntactically valid."""
    request = NotificationRequest(
        message='He said "hello"',
        title='Title with "quotes"',
        urgency=NotificationUrgency.NORMAL,
    )

    script = _build_osascript_notification(request, "agent-quotes")

    # Escaped quotes (\") must be present for message and title contents.
    assert 'He said \\"hello\\"' in script
    assert 'Title with \\"quotes\\"' in script
    # Raw unescaped quoted payload must not appear as a bare substring between
    # AppleScript string delimiters -- the contents must be escaped, not just
    # textually present from the surrounding AppleScript syntax.
    assert '"He said "hello""' not in script
    assert '"Title with "quotes""' not in script


@pytest.mark.parametrize(
    "is_electron,is_macos,expected",
    [
        (True, True, DispatchChannel.ELECTRON),
        (True, False, DispatchChannel.ELECTRON),
        (False, True, DispatchChannel.MACOS),
        (False, False, DispatchChannel.TKINTER),
    ],
)
def test_select_dispatch_channel(is_electron: bool, is_macos: bool, expected: DispatchChannel) -> None:
    """Electron wins when set; macOS takes over when not in Electron; tkinter otherwise."""
    assert _select_dispatch_channel(is_electron=is_electron, is_macos=is_macos) == expected


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


@pytest.mark.parametrize(
    "urgency",
    [NotificationUrgency.LOW, NotificationUrgency.NORMAL, NotificationUrgency.CRITICAL],
)
def test_build_toast_widgets_colors_indicator_by_urgency_and_renders_text(urgency: NotificationUrgency) -> None:
    """_build_toast_widgets colors the urgency indicator per _URGENCY_COLOR_BY_LEVEL
    and renders the agent name, title, and message into the content labels."""
    tk = _make_fake_tk()
    root = tk.Frame()

    frame, content = _build_toast_widgets(
        root=root,
        title="Build Failed",
        message="The CI pipeline did not complete.",
        urgency=urgency,
        agent_display_name="agent-build",
        tk=tk,
    )

    # The indicator is the first child Frame of the outer frame; its bg encodes urgency.
    indicator = frame.winfo_children()[0]
    assert indicator.kwargs["bg"] == _URGENCY_COLOR_BY_LEVEL[urgency]

    # The content frame holds the text labels; collect their rendered text.
    label_texts = [child.kwargs["text"] for child in content.winfo_children()]
    assert "From: agent-build" in label_texts
    assert "Build Failed" in label_texts
    assert "The CI pipeline did not complete." in label_texts


def test_position_toast_window_sets_computed_bottom_right_geometry() -> None:
    """_position_toast_window sets a WxH+X+Y geometry spec computed from the
    screen size, the window's requested height, and the bottom-right offsets."""
    tk = _make_fake_tk()
    root = tk.Frame()
    width = 320

    _position_toast_window(root, width=width)

    # Expected values mirror the production formula in _position_toast_window,
    # using the fake widget's reqheight/screen dimensions.
    height = root.winfo_reqheight()
    x_position = root.winfo_screenwidth() - width - 20
    y_position = root.winfo_screenheight() - height - 60
    assert root.geometry_spec == f"{width}x{height}+{x_position}+{y_position}"


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
