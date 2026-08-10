from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_notifications.notifier import LinuxNotifier
from imbue.mngr_notifications.notifier import Notifier


class RecordingNotifier(Notifier):
    """Test notifier that records calls instead of sending notifications."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def notify(self, title: str, message: str, execute_command: str | None, cg: ConcurrencyGroup) -> None:
        self.calls.append((title, message, execute_command))


class RecordingLinuxNotifier(LinuxNotifier):
    """A LinuxNotifier that records calls instead of invoking notify-send.

    Inherits from LinuxNotifier (not the abstract Notifier) so that code which
    dispatches on the concrete notifier type (e.g. run_test_notification's
    isinstance check) still routes to the Linux branch, while letting tests
    assert that notify was actually invoked.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def notify(self, title: str, message: str, execute_command: str | None, cg: ConcurrencyGroup) -> None:
        self.calls.append((title, message, execute_command))
