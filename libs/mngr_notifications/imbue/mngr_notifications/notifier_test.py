import shlex
from collections.abc import Generator

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_notifications.config import NotificationsPluginConfig
from imbue.mngr_notifications.notifier import LinuxNotifier
from imbue.mngr_notifications.notifier import MacOSNotifier
from imbue.mngr_notifications.notifier import build_execute_command
from imbue.mngr_notifications.notifier import get_notifier
from imbue.mngr_notifications.testing import patch_platform


def _config(
    terminal_app: str | None = None,
    custom_terminal_command: str | None = None,
    notification_only: bool = False,
) -> NotificationsPluginConfig:
    return NotificationsPluginConfig(
        terminal_app=terminal_app,
        custom_terminal_command=custom_terminal_command,
        notification_only=notification_only,
    )


@pytest.fixture()
def notification_cg() -> Generator[ConcurrencyGroup, None, None]:
    """ConcurrencyGroup for notification subprocess calls."""
    with ConcurrencyGroup(name="test-notification") as group:
        yield group


# --- build_execute_command ---


def test_build_execute_command_no_config() -> None:
    """No terminal_app or custom_command returns None."""
    assert build_execute_command("agent-x", _config()) is None


def test_build_execute_command_notification_only() -> None:
    """notification_only=True returns None even with terminal_app set."""
    assert build_execute_command("agent-x", _config(terminal_app="iTerm", notification_only=True)) is None


def test_build_execute_command_custom_command() -> None:
    """custom_terminal_command is used with MNGR_AGENT_NAME exported for shell expansion."""
    result = build_execute_command("agent-x", _config(custom_terminal_command="my-cmd $MNGR_AGENT_NAME"))
    assert result is not None
    assert result == "export MNGR_AGENT_NAME=agent-x && my-cmd $MNGR_AGENT_NAME"


def test_build_execute_command_custom_command_with_quotes_in_name() -> None:
    """Agent names with single quotes are properly escaped via shlex.quote."""
    result = build_execute_command("it's-agent", _config(custom_terminal_command="my-cmd"))
    assert result is not None
    expected_name = shlex.quote("it's-agent")
    assert result == f"export MNGR_AGENT_NAME={expected_name} && my-cmd"


def test_build_execute_command_custom_takes_precedence() -> None:
    """custom_terminal_command takes precedence over terminal_app."""
    result = build_execute_command(
        "agent-x",
        _config(terminal_app="iTerm", custom_terminal_command="my-cmd"),
    )
    assert result is not None
    assert "my-cmd" in result
    assert "iTerm" not in result


def test_build_execute_command_iterm() -> None:
    result = build_execute_command("agent-x", _config(terminal_app="iTerm"))
    assert result is not None
    assert "iTerm2" in result
    assert "mngr connect" in result
    assert "agent-x" in result


def test_build_execute_command_iterm2() -> None:
    result = build_execute_command("agent-x", _config(terminal_app="iterm2"))
    assert result is not None
    assert "iTerm2" in result


def test_build_execute_command_terminal_app() -> None:
    result = build_execute_command("agent-x", _config(terminal_app="Terminal"))
    assert result is not None
    assert '"Terminal"' in result
    assert "do script" in result


def test_build_execute_command_wezterm() -> None:
    result = build_execute_command("agent-x", _config(terminal_app="WezTerm"))
    assert result is not None
    assert "wezterm cli spawn" in result


def test_build_execute_command_kitty() -> None:
    result = build_execute_command("agent-x", _config(terminal_app="Kitty"))
    assert result is not None
    assert "kitty @" in result
    assert "--type=tab" in result


def test_build_execute_command_unsupported_terminal() -> None:
    result = build_execute_command("agent-x", _config(terminal_app="Hyper"))
    assert result is None


# --- get_notifier ---


def test_get_notifier_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_platform(monkeypatch, "Darwin")
    assert isinstance(get_notifier(), MacOSNotifier)


def test_get_notifier_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_platform(monkeypatch, "Linux")
    assert isinstance(get_notifier(), LinuxNotifier)


def test_get_notifier_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_platform(monkeypatch, "Windows")
    assert get_notifier() is None


# --- LinuxNotifier ---


def test_linux_notifier_rejects_execute_command(notification_cg: ConcurrencyGroup) -> None:
    with pytest.raises(NotImplementedError):
        LinuxNotifier().notify("Title", "Message", "some-command", notification_cg)
