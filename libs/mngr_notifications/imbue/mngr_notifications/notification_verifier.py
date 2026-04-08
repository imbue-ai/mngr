import shutil
from collections.abc import Callable
from typing import Final

from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_notifications.notifier import ALERTER_SYSTEM_CLICK_RESPONSES
from imbue.mngr_notifications.notifier import LinuxNotifier
from imbue.mngr_notifications.notifier import MacOSNotifier
from imbue.mngr_notifications.notifier import Notifier

DEFAULT_VERIFY_TIMEOUT: Final[float] = 15.0
_TEST_TITLE: Final[str] = "mngr notify test"
_TEST_MESSAGE_CLICK: Final[str] = "Click this notification to verify delivery"
_TEST_MESSAGE_BASIC: Final[str] = "Test notification from mngr notify"
_VERIFY_ACTION_LABEL: Final[str] = "OK"
_ALERTER_VERIFY_CLICKED_RESPONSES: Final[frozenset[str]] = ALERTER_SYSTEM_CLICK_RESPONSES | {_VERIFY_ACTION_LABEL}


class VerifyNotificationResult(FrozenModel):
    """Result of a test notification attempt."""

    is_sent: bool = Field(description="Whether the notification was sent without error")
    is_clicked: bool | None = Field(
        default=None,
        description="Whether the user clicked the notification. None if click detection is not supported.",
    )
    error_message: str | None = Field(default=None, description="Error message if sending failed")


def check_notifier_binary(notifier: Notifier) -> str | None:
    """Check if the notification binary is available. Returns an error message if not, None if OK."""
    match notifier:
        case MacOSNotifier():
            if shutil.which("alerter") is None:
                return "alerter not found; install with: brew install vjeantet/tap/alerter"
            return None
        case LinuxNotifier():
            if shutil.which("notify-send") is None:
                return "notify-send not found; install libnotify to enable notifications"
            return None
        case _:
            return f"Unsupported notifier type: {type(notifier).__name__}"


def run_test_notification(
    notifier: Notifier,
    cg: ConcurrencyGroup,
    verify_timeout: float = DEFAULT_VERIFY_TIMEOUT,
    binary_checker: Callable[[Notifier], str | None] = check_notifier_binary,
) -> VerifyNotificationResult:
    """Send a test notification and check that it was delivered.

    On macOS, uses alerter in blocking mode with an action button and checks
    whether the user clicked. On Linux, sends the notification and returns
    is_clicked=None (caller should prompt the user to confirm).
    """
    binary_error = binary_checker(notifier)
    if binary_error is not None:
        return VerifyNotificationResult(is_sent=False, error_message=binary_error)

    if isinstance(notifier, MacOSNotifier):
        return _run_alerter_verification(cg, verify_timeout)

    notifier.notify(_TEST_TITLE, _TEST_MESSAGE_BASIC, None, cg)
    return VerifyNotificationResult(is_sent=True, is_clicked=None)


def _run_alerter_verification(
    cg: ConcurrencyGroup,
    verify_timeout: float,
) -> VerifyNotificationResult:
    """Send a test notification via alerter and check if the user clicked it."""
    cmd = [
        "alerter",
        "--title",
        _TEST_TITLE,
        "--message",
        _TEST_MESSAGE_CLICK,
        "--actions",
        _VERIFY_ACTION_LABEL,
        "--timeout",
        str(int(verify_timeout)),
    ]
    try:
        result = cg.run_process_to_completion(cmd, timeout=verify_timeout + 5, is_checked_after=False)
    except (FileNotFoundError, OSError):
        return VerifyNotificationResult(
            is_sent=False,
            error_message="alerter not found; install with: brew install vjeantet/tap/alerter",
        )

    is_clicked = result.stdout.strip() in _ALERTER_VERIFY_CLICKED_RESPONSES
    return VerifyNotificationResult(is_sent=True, is_clicked=is_clicked)
