from collections.abc import Sequence

import click

from imbue.mng.config.plugin_registry import get_plugin_config_class
from imbue.mng_notifications.config import NotificationsPluginConfig
from imbue.mng_notifications.plugin import register_cli_commands


def test_register_cli_commands_returns_notify_command() -> None:
    """Verify that register_cli_commands returns the notify command."""
    result = register_cli_commands()

    assert result is not None
    assert isinstance(result, Sequence)
    assert len(result) == 1
    assert isinstance(result[0], click.Command)
    assert result[0].name == "notify"


def test_plugin_config_is_registered() -> None:
    """Verify that the notifications plugin config is registered."""
    config_class = get_plugin_config_class("notifications")
    assert config_class is NotificationsPluginConfig
