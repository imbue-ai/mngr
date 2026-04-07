import platform
import shlex
from abc import ABC
from abc import abstractmethod
from typing import Final

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_notifications.config import NotificationsPluginConfig
from imbue.mngr_notifications.terminals import get_terminal_app

_ALERTER_TIMEOUT: Final[int] = 30
_ALERTER_ACTION_LABEL: Final[str] = "Connect"
_ALERTER_CLICKED_RESPONSES: Final[frozenset[str]] = frozenset(
    {"@CONTENTCLICKED", "@ACTIONCLICKED", _ALERTER_ACTION_LABEL}
)


class Notifier(ABC):
    """Sends desktop notifications."""

    @abstractmethod
    def notify(self, title: str, message: str, execute_command: str | None, cg: ConcurrencyGroup) -> None:
        """Send a notification with an optional click action."""


class MacOSNotifier(Notifier):
    """Sends notifications on macOS via alerter.

    Uses alerter (brew install vjeantet/tap/alerter) which supports action
    buttons and reports click results via stdout. When an execute_command is
    provided, alerter blocks until the user interacts, then runs the command
    on click. Without an execute_command, the notification is fire-and-forget.
    """

    def notify(self, title: str, message: str, execute_command: str | None, cg: ConcurrencyGroup) -> None:
        cmd = ["alerter", "--title", title, "--message", message, "--timeout", str(_ALERTER_TIMEOUT)]

        if execute_command is None:
            # No action needed on click -- fire and forget
            try:
                cg.run_process_in_background(cmd)
            except FileNotFoundError:
                logger.warning("alerter not found; install with: brew install vjeantet/tap/alerter")
            return

        # Add action button and block until user interacts or timeout
        cmd.extend(["--actions", _ALERTER_ACTION_LABEL])
        try:
            result = cg.run_process_to_completion(cmd, timeout=_ALERTER_TIMEOUT + 5, is_checked_after=False)
            if result.stdout.strip() in _ALERTER_CLICKED_RESPONSES:
                cg.run_process_in_background(["sh", "-c", execute_command])
        except FileNotFoundError:
            logger.warning("alerter not found; install with: brew install vjeantet/tap/alerter")


class LinuxNotifier(Notifier):
    """Sends notifications on Linux via notify-send."""

    def notify(self, title: str, message: str, execute_command: str | None, cg: ConcurrencyGroup) -> None:
        if execute_command is not None:
            raise NotImplementedError("notify-send does not support click actions; use notification_only = true")
        try:
            cg.run_process_to_completion(["notify-send", title, message], timeout=10, is_checked_after=False)
        except FileNotFoundError:
            logger.warning("notify-send not found; install libnotify to enable notifications")


def get_notifier() -> Notifier | None:
    """Return the appropriate notifier for the current platform, or None if unsupported."""
    system = platform.system()
    if system == "Darwin":
        return MacOSNotifier()
    if system == "Linux":
        return LinuxNotifier()
    logger.warning("Desktop notifications not supported on {}", system)
    return None


def build_execute_command(agent_name: str, config: NotificationsPluginConfig) -> str | None:
    """Build the shell command to run when the notification is clicked.

    Returns None if no terminal_app or custom_terminal_command is configured.
    """
    if config.notification_only:
        return None

    if config.custom_terminal_command is not None:
        quoted_name = shlex.quote(agent_name)
        return f"export MNGR_AGENT_NAME={quoted_name} && {config.custom_terminal_command}"

    if config.terminal_app is None:
        return None

    terminal = get_terminal_app(config.terminal_app)
    if terminal is None:
        logger.warning(
            "Unsupported terminal app: {}. Use custom_terminal_command instead.",
            config.terminal_app,
        )
        return None

    quoted_name = shlex.quote(agent_name)
    mngr_connect = f"mngr connect {quoted_name}"
    return terminal.build_connect_command(mngr_connect, agent_name)
