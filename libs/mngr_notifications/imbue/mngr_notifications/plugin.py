from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr_notifications.cli import notify
from imbue.mngr_notifications.config import NotificationsPluginConfig

register_plugin_config("notifications", NotificationsPluginConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the notify command with mngr."""
    return [notify]
