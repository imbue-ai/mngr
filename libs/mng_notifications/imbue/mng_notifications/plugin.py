from collections.abc import Sequence

import click

from imbue.mng import hookimpl
from imbue.mng.config.plugin_registry import register_plugin_config
from imbue.mng_notifications.cli import notify
from imbue.mng_notifications.config import NotificationsPluginConfig

register_plugin_config("notifications", NotificationsPluginConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the notify command with mng."""
    return [notify]
