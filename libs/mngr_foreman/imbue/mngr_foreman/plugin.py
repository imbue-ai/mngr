"""Plugin entry point: registers the ``mngr foreman`` CLI group."""

from collections.abc import Sequence

import click

from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr_foreman import hookimpl
from imbue.mngr_foreman.cli.group import foreman as foreman_group
from imbue.mngr_foreman.config import ForemanPluginConfig

register_plugin_config("foreman", ForemanPluginConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command]:
    """Register the top-level ``mngr foreman`` command group."""
    return [foreman_group]
