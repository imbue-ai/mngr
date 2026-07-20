from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr_specs.cli import specs


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the specs command group with mngr."""
    return [specs]
