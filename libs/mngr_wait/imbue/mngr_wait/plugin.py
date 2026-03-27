from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr_wait.cli import wait


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the wait command with mngr."""
    return [wait]
