from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr_tutor.cli import tutor


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the tutor command with mngr."""
    return [tutor]
