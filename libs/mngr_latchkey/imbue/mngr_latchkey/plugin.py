"""Plugin entry point: registers the ``mngr latchkey`` CLI group and config block."""

from collections.abc import Sequence

import click

from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr_latchkey import hookimpl
from imbue.mngr_latchkey.cli import latchkey as latchkey_group
from imbue.mngr_latchkey.config import LatchkeyPluginConfig

register_plugin_config("latchkey", LatchkeyPluginConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command]:
    """Register the top-level ``mngr latchkey`` command group."""
    return [latchkey_group]
