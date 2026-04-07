from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_notifications.cli import _run_verification
from imbue.mngr_notifications.notification_verifier import _build_marker_touch_command
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


class _ClickSimulatingNotifier(MacOSNotifier):
    """A MacOSNotifier subclass that simulates the user clicking the notification."""

    def notify(self, title: str, message: str, execute_command: str | None, cg: ConcurrencyGroup) -> None:
        if execute_command is not None:
            # Simulate click by executing the command directly
            cg.run_process_to_completion(["sh", "-c", execute_command], timeout=5)


class _SilentMacOSNotifier(MacOSNotifier):
    """A MacOSNotifier subclass that silently does nothing (simulates notification not seen)."""

    def notify(self, title: str, message: str, execute_command: str | None, cg: ConcurrencyGroup) -> None:
        pass


class _NoOpLinuxNotifier(LinuxNotifier):
    """A LinuxNotifier subclass that silently does nothing (simulates successful send)."""

    def notify(self, title: str, message: str, execute_command: str | None, cg: ConcurrencyGroup) -> None:
        if execute_command is not None:
            raise NotImplementedError("notify-send does not support click actions")


# --- run_test_notification ---


def test_run_test_notification_click_verified(notification_cg: ConcurrencyGroup) -> None:
    """Click verification succeeds when the notification execute command creates the marker."""
    notifier = _ClickSimulatingNotifier()
    result = run_test_notification(notifier, notification_cg, click_timeout=5.0, binary_checker=_no_binary_issues)

    assert result.is_sent is True
    assert result.is_clicked is True
    assert result.error_message is None


def test_run_test_notification_click_not_detected(notification_cg: ConcurrencyGroup) -> None:
    """Click verification fails when the notification is not clicked."""
    notifier = _SilentMacOSNotifier()
    result = run_test_notification(notifier, notification_cg, click_timeout=1.0, binary_checker=_no_binary_issues)

    assert result.is_sent is True
    assert result.is_clicked is False
    assert result.error_message is None


def test_run_test_notification_linux_no_click_verification(notification_cg: ConcurrencyGroup) -> None:
    """Linux notifier returns is_clicked=None (no click detection)."""
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


# --- _build_marker_touch_command ---


def test_build_marker_touch_command() -> None:
    path = Path("/tmp/mngr-notify-test-abc123")
    cmd = _build_marker_touch_command(path)
    assert cmd == "touch /tmp/mngr-notify-test-abc123"


def test_build_marker_touch_command_quotes_spaces() -> None:
    path = Path("/tmp/path with spaces/marker")
    cmd = _build_marker_touch_command(path)
    assert cmd == "touch '/tmp/path with spaces/marker'"


# --- check_notifier_binary ---


def test_check_notifier_binary_macos() -> None:
    """On macOS (where we're running tests), terminal-notifier should be found."""
    result = check_notifier_binary(MacOSNotifier())
    # terminal-notifier is installed in the test environment
    assert result is None


def test_check_notifier_binary_linux_missing_on_macos() -> None:
    """On macOS, notify-send is not installed so Linux check returns error."""
    result = check_notifier_binary(LinuxNotifier())
    assert result is not None
    assert "notify-send" in result


# --- _run_verification (CLI integration) ---


def test_run_verification_click_verified(notification_cg: ConcurrencyGroup) -> None:
    """_run_verification returns True when click verification succeeds."""
    notifier = _ClickSimulatingNotifier()
    result = _run_verification(notifier, notification_cg, binary_checker=_no_binary_issues, click_timeout=5.0)
    assert result is True


def test_run_verification_click_not_detected(notification_cg: ConcurrencyGroup) -> None:
    """_run_verification returns False when notification is not clicked."""
    notifier = _SilentMacOSNotifier()
    result = _run_verification(notifier, notification_cg, binary_checker=_no_binary_issues, click_timeout=1.0)
    assert result is False


def test_run_verification_send_failed(notification_cg: ConcurrencyGroup) -> None:
    """_run_verification returns False when notification sending fails."""
    notifier = MacOSNotifier()
    result = _run_verification(notifier, notification_cg, binary_checker=_binary_always_missing)
    assert result is False
