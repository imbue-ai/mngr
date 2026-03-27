import platform
import shlex
from abc import ABC
from abc import abstractmethod

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng_notifications.config import NotificationsPluginConfig
from imbue.mng_notifications.terminals import get_terminal_app


class Notifier(ABC):
    """Sends desktop notifications."""

    @abstractmethod
    def notify(self, title: str, message: str, execute_command: str | None, cg: ConcurrencyGroup) -> None:
        """Send a notification with an optional click action."""


class MacOSNotifier(Notifier):
    """Sends notifications on macOS via terminal-notifier."""

    def notify(self, title: str, message: str, execute_command: str | None, cg: ConcurrencyGroup) -> None:
        cmd = ["terminal-notifier", "-title", title, "-message", message]
        if execute_command is not None:
            cmd.extend(["-execute", execute_command])
        try:
            cg.run_process_to_completion(cmd, timeout=10, is_checked_after=False)
        except FileNotFoundError:
            logger.warning("terminal-notifier not found; install with: brew install terminal-notifier")


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
        return f"export MNG_AGENT_NAME={quoted_name} && {config.custom_terminal_command}"

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
    mng_connect = f"mng connect {quoted_name}"
    return terminal.build_connect_command(mng_connect, agent_name)
