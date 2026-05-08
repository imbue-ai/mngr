from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr_latchkey.cli import latchkey


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the ``mngr latchkey`` command group."""
    return [latchkey]
