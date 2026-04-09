import shutil

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_notifications.cli import _run_verification
from imbue.mngr_notifications.notification_verifier import VerifyNotificationResult
from imbue.mngr_notifications.notification_verifier import check_notifier_binary
from imbue.mngr_notifications.notification_verifier import run_test_notification
from imbue.mngr_notifications.notifier import LinuxNotifier
from imbue.mngr_notifications.notifier import MacOSNotifier
from imbue.mngr_notifications.notifier import Notifier


def _no_binary_issues(notifier: Notifier) -> str | None:
    """Binary checker that always succeeds."""
    return None


def _binary_always_missing(notifier: Notifier) -> str | None:
    """Binary checker that always reports missing binary."""
    return "notifier binary not found"


class _NoOpMacOSNotifier(MacOSNotifier):
    """A MacOSNotifier subclass that silently does nothing."""

    def notify(self, title: str, message: str, execute_command: str | None, cg: ConcurrencyGroup) -> None:
        pass


class _NoOpLinuxNotifier(LinuxNotifier):
    """A LinuxNotifier subclass that silently does nothing."""

    def notify(self, title: str, message: str, execute_command: str | None, cg: ConcurrencyGroup) -> None:
        pass


class _UnsupportedNotifier(Notifier):
    """A notifier that is neither macOS nor Linux."""

    def notify(self, title: str, message: str, execute_command: str | None, cg: ConcurrencyGroup) -> None:
        pass


# --- run_test_notification ---


@pytest.mark.skipif(shutil.which("alerter") is not None, reason="alerter is installed")
def test_run_test_notification_macos_alerter_missing(notification_cg: ConcurrencyGroup) -> None:
    """When alerter is not installed, binary checker catches it before subprocess launch."""
    notifier = _NoOpMacOSNotifier()
    # Use the real binary checker -- it detects alerter is missing and returns early
    result = run_test_notification(notifier, notification_cg, verify_timeout=1.0)

    assert result.is_sent is False
    assert result.error_message is not None
    assert "alerter" in result.error_message


def test_run_test_notification_linux(notification_cg: ConcurrencyGroup) -> None:
    """Linux notifier sends and returns is_clicked=None."""
    notifier = _NoOpLinuxNotifier()
    result = run_test_notification(notifier, notification_cg, binary_checker=_no_binary_issues)

    assert result.is_sent is True
    assert result.is_clicked is None
    assert result.error_message is None


def test_run_test_notification_binary_check_fails(notification_cg: ConcurrencyGroup) -> None:
    """When the binary check fails, run_test_notification returns early without calling notify."""
    notifier = MacOSNotifier()
    result = run_test_notification(notifier, notification_cg, binary_checker=_binary_always_missing)

    assert result.is_sent is False
    assert result.error_message == "notifier binary not found"


# --- check_notifier_binary ---


@pytest.mark.skipif(shutil.which("alerter") is None, reason="alerter not installed")
def test_check_notifier_binary_macos() -> None:
    """check_notifier_binary returns None when alerter is available."""
    result = check_notifier_binary(MacOSNotifier())
    assert result is None


@pytest.mark.skipif(shutil.which("alerter") is not None, reason="alerter is installed")
def test_check_notifier_binary_macos_missing() -> None:
    """check_notifier_binary returns error when alerter is not found."""
    result = check_notifier_binary(MacOSNotifier())
    assert result is not None
    assert "alerter" in result


@pytest.mark.skipif(shutil.which("notify-send") is not None, reason="notify-send is installed")
def test_check_notifier_binary_linux_missing() -> None:
    """check_notifier_binary returns error when notify-send is not found."""
    result = check_notifier_binary(LinuxNotifier())
    assert result is not None
    assert "notify-send" in result


@pytest.mark.skipif(shutil.which("notify-send") is None, reason="notify-send not installed")
def test_check_notifier_binary_linux_present() -> None:
    """check_notifier_binary returns None when notify-send is available."""
    result = check_notifier_binary(LinuxNotifier())
    assert result is None


def test_check_notifier_binary_unsupported_type() -> None:
    """check_notifier_binary returns error for unknown notifier types."""
    result = check_notifier_binary(_UnsupportedNotifier())
    assert result is not None
    assert "_UnsupportedNotifier" in result


# --- _run_verification (CLI integration) ---


def test_run_verification_send_failed(notification_cg: ConcurrencyGroup) -> None:
    """_run_verification returns False when notification sending fails."""
    notifier = MacOSNotifier()
    result = _run_verification(notifier, notification_cg, binary_checker=_binary_always_missing)
    assert result is False


def test_run_verification_linux_confirmed(notification_cg: ConcurrencyGroup) -> None:
    """_run_verification returns True when user confirms they saw the notification on Linux."""
    notifier = _NoOpLinuxNotifier()
    result = _run_verification(
        notifier, notification_cg, binary_checker=_no_binary_issues, confirm_fn=lambda _prompt: True
    )
    assert result is True


def test_run_verification_linux_not_confirmed(notification_cg: ConcurrencyGroup) -> None:
    """_run_verification returns False when user denies seeing the notification on Linux."""
    notifier = _NoOpLinuxNotifier()
    result = _run_verification(
        notifier, notification_cg, binary_checker=_no_binary_issues, confirm_fn=lambda _prompt: False
    )
    assert result is False


def test_run_verification_click_verified(notification_cg: ConcurrencyGroup) -> None:
    """_run_verification returns True when click detection reports success."""
    notifier = _NoOpMacOSNotifier()
    clicked_result = VerifyNotificationResult(is_sent=True, is_clicked=True)
    result = _run_verification(notifier, notification_cg, override_result=clicked_result)
    assert result is True


def test_run_verification_click_not_detected(notification_cg: ConcurrencyGroup) -> None:
    """_run_verification returns False when click detection reports timeout."""
    notifier = _NoOpMacOSNotifier()
    timeout_result = VerifyNotificationResult(is_sent=True, is_clicked=False)
    result = _run_verification(notifier, notification_cg, override_result=timeout_result)
    assert result is False
