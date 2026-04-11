"""Notification dispatch for the minds desktop client.

Routes notifications to either Electron (stdout JSONL), native macOS
notifications, or a tkinter toast popup depending on the runtime context.
"""

import platform
import threading
from enum import auto
from types import ModuleType
from typing import Any

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.primitives import OutputFormat
from imbue.minds.utils.output import emit_event

_IS_MACOS: bool = platform.system() == "Darwin"

# tkinter is an optional dependency: not available on all platforms (e.g. headless servers).
# Load it at module level so the failure is immediate and predictable, but tolerate absence.
_TKINTER: ModuleType | None
try:
    _TKINTER = __import__("tkinter")
except ImportError:
    _TKINTER = None


class NotificationUrgency(UpperCaseStrEnum):
    """Urgency level for a user notification."""

    LOW = auto()
    NORMAL = auto()
    CRITICAL = auto()


class NotificationRequest(FrozenModel):
    """A notification to display to the user."""

    message: str = Field(description="Notification body text")
    title: str | None = Field(default=None, description="Optional notification title")
    urgency: NotificationUrgency = Field(
        default=NotificationUrgency.NORMAL,
        description="Urgency level (low, normal, critical)",
    )


_URGENCY_COLOR_BY_LEVEL: dict[NotificationUrgency, str] = {
    NotificationUrgency.LOW: "#22c55e",
    NotificationUrgency.NORMAL: "#eab308",
    NotificationUrgency.CRITICAL: "#ef4444",
}


def _dispatch_electron_notification(
    request: NotificationRequest,
    agent_display_name: str,
) -> None:
    """Write notification as JSONL to stdout for the Electron main process."""
    data: dict[str, str] = {
        "message": request.message,
        "urgency": str(request.urgency),
        "agent_name": agent_display_name,
    }
    if request.title is not None:
        data["title"] = request.title
    emit_event("notification", data, OutputFormat.JSONL)


def _build_toast_widgets(
    root: Any,
    title: str,
    message: str,
    urgency: NotificationUrgency,
    agent_display_name: str,
    tk: ModuleType,
) -> tuple[Any, Any]:
    """Build the widget tree for a toast notification window.

    Returns (frame, content) where frame is the outermost container and
    content holds the text labels (used for click binding).
    """
    urgency_color = _URGENCY_COLOR_BY_LEVEL.get(urgency, "#eab308")

    frame = tk.Frame(root, bg="#1e293b", padx=12, pady=8)
    frame.pack(fill=tk.BOTH, expand=True)

    indicator = tk.Frame(frame, bg=urgency_color, width=4)
    indicator.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))

    content = tk.Frame(frame, bg="#1e293b")
    content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    tk.Label(
        content,
        text=f"From: {agent_display_name}",
        fg="#94a3b8",
        bg="#1e293b",
        font=("sans-serif", 9),
        anchor="w",
    ).pack(fill=tk.X)

    tk.Label(
        content,
        text=title,
        fg="#f1f5f9",
        bg="#1e293b",
        font=("sans-serif", 11, "bold"),
        anchor="w",
    ).pack(fill=tk.X)

    tk.Label(
        content,
        text=message,
        fg="#cbd5e1",
        bg="#1e293b",
        font=("sans-serif", 10),
        anchor="w",
        wraplength=280,
        justify=tk.LEFT,
    ).pack(fill=tk.X, pady=(4, 0))

    tk.Label(
        content,
        text="Click to dismiss",
        fg="#64748b",
        bg="#1e293b",
        font=("sans-serif", 8),
        anchor="w",
    ).pack(fill=tk.X, pady=(4, 0))

    return frame, content


def _position_toast_window(root: Any, width: int = 320) -> None:
    """Position a toast window in the bottom-right corner of the screen."""
    root.update_idletasks()
    height = root.winfo_reqheight()
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    x_position = screen_width - width - 20
    y_position = screen_height - height - 60
    root.geometry(f"{width}x{height}+{x_position}+{y_position}")


def _run_tkinter_toast(
    title: str,
    message: str,
    urgency: NotificationUrgency,
    agent_display_name: str,
    tk: ModuleType | None,
) -> None:
    """Create and display a tkinter toast window. Runs on a background thread."""
    if tk is None:
        logger.warning("tkinter not available, cannot show notification toast")
        return
    try:
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)

        frame, content = _build_toast_widgets(root, title, message, urgency, agent_display_name, tk)
        _position_toast_window(root)

        root.bind("<Button-1>", lambda _event: root.destroy())
        frame.bind("<Button-1>", lambda _event: root.destroy())
        for child in content.winfo_children():
            child.bind("<Button-1>", lambda _event: root.destroy())

        root.mainloop()
    except (tk.TclError, OSError, RuntimeError) as e:
        logger.warning("Failed to show tkinter notification: {}", e)


def _show_tkinter_toast(
    request: NotificationRequest,
    agent_display_name: str,
    tk: ModuleType | None,
) -> None:
    """Show a small always-on-top toast window in the bottom-right corner."""
    display_title = request.title or "Notification"
    thread = threading.Thread(
        target=_run_tkinter_toast,
        args=(display_title, request.message, request.urgency, agent_display_name, tk),
        daemon=True,
        name="tkinter-toast",
    )
    thread.start()


def _dispatch_macos_notification(
    request: NotificationRequest,
    agent_display_name: str,
) -> None:
    """Display a native macOS notification via osascript."""
    display_title = request.title or f"Notification from {agent_display_name}"
    # Escape double quotes for AppleScript string literals
    escaped_title = display_title.replace('"', '\\"')
    escaped_message = request.message.replace('"', '\\"')
    escaped_subtitle = f"From: {agent_display_name}".replace('"', '\\"')
    script = f'display notification "{escaped_message}" with title "{escaped_title}" subtitle "{escaped_subtitle}"'
    cg = ConcurrencyGroup(name="macos-notification")
    try:
        with cg:
            cg.run_process_to_completion(
                command=["osascript", "-e", script],
                is_checked_after=False,
            )
    except (OSError, ExceptionGroup) as e:
        logger.warning("Failed to show macOS notification: {}", e)


class NotificationDispatcher(FrozenModel):
    """Routes notifications to Electron, macOS native, or tkinter based on runtime context."""

    is_electron: bool = Field(description="Whether the server is running inside Electron")
    is_macos: bool = Field(default=_IS_MACOS, description="Whether running on macOS")
    # _tk stores the resolved tkinter module. Set at construction time to the
    # module-level _TKINTER value, or injected via NotificationDispatcher.create()
    # to allow testing without tkinter side effects.
    _tk: ModuleType | None = PrivateAttr(default=None)

    def model_post_init(self, __context: object) -> None:
        """Resolve the tkinter module from the module-level auto-detection."""
        self._tk = _TKINTER

    @classmethod
    def create(
        cls,
        is_electron: bool,
        tkinter_module: ModuleType | None = _TKINTER,
        is_macos: bool = _IS_MACOS,
    ) -> "NotificationDispatcher":
        """Create a NotificationDispatcher with explicit platform overrides.

        Pass tkinter_module=None to disable tkinter toasts (e.g. in tests or on
        headless servers where tkinter is unavailable).
        """
        dispatcher = cls(is_electron=is_electron, is_macos=is_macos)
        dispatcher._tk = tkinter_module
        return dispatcher

    def dispatch(
        self,
        request: NotificationRequest,
        agent_display_name: str,
    ) -> None:
        """Send a notification to the user via the appropriate channel.

        Priority: Electron > macOS native > tkinter toast.
        """
        if self.is_electron:
            _dispatch_electron_notification(request, agent_display_name)
        elif self.is_macos:
            _dispatch_macos_notification(request, agent_display_name)
        else:
            _show_tkinter_toast(request, agent_display_name, tk=self._tk)
