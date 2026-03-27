from collections.abc import Sequence

import click

from imbue.mng import hookimpl
from imbue.mng.config.plugin_registry import register_plugin_config
from imbue.mng_kanpan.cli import kanpan
from imbue.mng_kanpan.data_types import KanpanPluginConfig

register_plugin_config("kanpan", KanpanPluginConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the kanpan command with mng."""
    return [kanpan]
