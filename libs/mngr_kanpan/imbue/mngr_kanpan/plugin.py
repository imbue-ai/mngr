from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr_kanpan.cli import kanpan
from imbue.mngr_kanpan.data_types import KanpanPluginConfig

register_plugin_config("kanpan", KanpanPluginConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the kanpan command with mngr."""
    return [kanpan]
