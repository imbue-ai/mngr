"""Notification dispatch for the minds desktop client.

Routes notifications to either Electron (stdout JSONL) or a tkinter
toast popup depending on whether the server is running inside the
Electron desktop app.
"""

import threading
from enum import auto

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.primitives import OutputFormat
from imbue.minds.utils.output import emit_event


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


def _run_tkinter_toast(
    title: str,
    message: str,
    urgency: NotificationUrgency,
    agent_display_name: str,
) -> None:
    """Create and display a tkinter toast window. Runs on a background thread."""
    try:
        import tkinter as tk
    except ImportError as e:
        logger.warning("tkinter not available, cannot show notification: {}", e)
        return

    try:
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)

        urgency_color = _URGENCY_COLOR_BY_LEVEL.get(urgency, "#eab308")

        frame = tk.Frame(root, bg="#1e293b", padx=12, pady=8)
        frame.pack(fill=tk.BOTH, expand=True)

        # Urgency indicator bar
        indicator = tk.Frame(frame, bg=urgency_color, width=4)
        indicator.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))

        content = tk.Frame(frame, bg="#1e293b")
        content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Agent name
        tk.Label(
            content,
            text=f"From: {agent_display_name}",
            fg="#94a3b8",
            bg="#1e293b",
            font=("sans-serif", 9),
            anchor="w",
        ).pack(fill=tk.X)

        # Title
        tk.Label(
            content,
            text=title,
            fg="#f1f5f9",
            bg="#1e293b",
            font=("sans-serif", 11, "bold"),
            anchor="w",
        ).pack(fill=tk.X)

        # Message
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

        # Dismiss hint
        tk.Label(
            content,
            text="Click to dismiss",
            fg="#64748b",
            bg="#1e293b",
            font=("sans-serif", 8),
            anchor="w",
        ).pack(fill=tk.X, pady=(4, 0))

        # Position in bottom-right corner
        root.update_idletasks()
        width = 320
        height = root.winfo_reqheight()
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        x_position = screen_width - width - 20
        y_position = screen_height - height - 60
        root.geometry(f"{width}x{height}+{x_position}+{y_position}")

        # Click anywhere to dismiss
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
) -> None:
    """Show a small always-on-top toast window in the bottom-right corner."""
    display_title = request.title or "Notification"
    thread = threading.Thread(
        target=_run_tkinter_toast,
        args=(display_title, request.message, request.urgency, agent_display_name),
        daemon=True,
        name="tkinter-toast",
    )
    thread.start()


class NotificationDispatcher(FrozenModel):
    """Routes notifications to Electron or tkinter based on runtime context."""

    is_electron: bool = Field(description="Whether the server is running inside Electron")

    def dispatch(
        self,
        request: NotificationRequest,
        agent_display_name: str,
    ) -> None:
        """Send a notification to the user via the appropriate channel."""
        if self.is_electron:
            _dispatch_electron_notification(request, agent_display_name)
        else:
            _show_tkinter_toast(request, agent_display_name)
