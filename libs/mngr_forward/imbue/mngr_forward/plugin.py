"""Plugin entry point: registers the ``mngr forward`` CLI command."""

from collections.abc import Sequence

import click

from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr_forward import hookimpl
from imbue.mngr_forward.cli import forward as forward_command
from imbue.mngr_forward.config import ForwardPluginConfig

register_plugin_config("forward", ForwardPluginConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command]:
    """Register the top-level ``mngr forward`` command."""
    return [forward_command]
