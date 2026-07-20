"""Plugin entry point: registers the ``mngr foreman`` CLI command."""

from collections.abc import Sequence

import click

from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr_foreman import hookimpl
from imbue.mngr_foreman.cli import foreman as foreman_command
from imbue.mngr_foreman.config import ForemanPluginConfig

register_plugin_config("foreman", ForemanPluginConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command]:
    """Register the top-level ``mngr foreman`` command."""
    return [foreman_command]
