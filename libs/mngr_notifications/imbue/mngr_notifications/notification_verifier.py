import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Final
from uuid import uuid4

from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_notifications.notifier import MacOSNotifier
from imbue.mngr_notifications.notifier import Notifier

DEFAULT_CLICK_TIMEOUT: Final[float] = 15.0
_CLICK_POLL_INTERVAL: Final[float] = 1.0
_TEST_TITLE: Final[str] = "mngr notify test"
_TEST_MESSAGE_CLICK: Final[str] = "Click this notification to verify delivery"
_TEST_MESSAGE_BASIC: Final[str] = "Test notification from mngr notify"
_MARKER_PREFIX: Final[str] = "mngr-notify-test-"


class VerifyNotificationResult(FrozenModel):
    """Result of a test notification attempt."""

    is_sent: bool = Field(description="Whether the notification was sent without error")
    is_clicked: bool | None = Field(
        default=None,
        description="Whether the user clicked the notification. None if click detection is not supported.",
    )
    error_message: str | None = Field(default=None, description="Error message if sending failed")


@pure
def _build_marker_touch_command(marker_path: Path) -> str:
    """Build a shell command that creates a marker file when executed."""
    return f"touch {marker_path}"


def check_notifier_binary(notifier: Notifier) -> str | None:
    """Check if the notification binary is available. Returns an error message if not, None if OK."""
    if isinstance(notifier, MacOSNotifier):
        if shutil.which("terminal-notifier") is None:
            return "terminal-notifier not found; install with: brew install terminal-notifier"
        return None

    # LinuxNotifier uses notify-send
    if shutil.which("notify-send") is None:
        return "notify-send not found; install libnotify to enable notifications"
    return None


def run_test_notification(
    notifier: Notifier,
    cg: ConcurrencyGroup,
    click_timeout: float = DEFAULT_CLICK_TIMEOUT,
    binary_checker: Callable[[Notifier], str | None] = check_notifier_binary,
) -> VerifyNotificationResult:
    """Send a test notification and optionally verify the user clicked it.

    On macOS with terminal-notifier, uses the -execute flag to touch a marker
    file when clicked, then polls for the marker. On Linux (or when click
    detection is unavailable), sends the notification and returns is_clicked=None.
    """
    binary_error = binary_checker(notifier)
    if binary_error is not None:
        return VerifyNotificationResult(is_sent=False, error_message=binary_error)

    if isinstance(notifier, MacOSNotifier):
        return _run_click_verified_test(notifier, cg, click_timeout)

    return _run_basic_test(notifier, cg)


def _run_click_verified_test(
    notifier: MacOSNotifier,
    cg: ConcurrencyGroup,
    click_timeout: float,
) -> VerifyNotificationResult:
    """Send a test notification with click verification via a marker file."""
    marker_path = Path(tempfile.gettempdir()) / f"{_MARKER_PREFIX}{uuid4().hex}"
    execute_command = _build_marker_touch_command(marker_path)

    try:
        notifier.notify(_TEST_TITLE, _TEST_MESSAGE_CLICK, execute_command, cg)
    except FileNotFoundError:
        return VerifyNotificationResult(
            is_sent=False,
            error_message="terminal-notifier not found; install with: brew install terminal-notifier",
        )

    try:
        is_clicked = poll_until(
            condition=lambda: marker_path.exists(),
            timeout=click_timeout,
            poll_interval=_CLICK_POLL_INTERVAL,
        )
        return VerifyNotificationResult(is_sent=True, is_clicked=is_clicked)
    finally:
        marker_path.unlink(missing_ok=True)


def _run_basic_test(
    notifier: Notifier,
    cg: ConcurrencyGroup,
) -> VerifyNotificationResult:
    """Send a test notification without click verification."""
    try:
        notifier.notify(_TEST_TITLE, _TEST_MESSAGE_BASIC, None, cg)
    except FileNotFoundError:
        return VerifyNotificationResult(
            is_sent=False,
            error_message="notify-send not found; install libnotify to enable notifications",
        )

    return VerifyNotificationResult(is_sent=True, is_clicked=None)
