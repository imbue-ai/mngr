from collections.abc import Sequence

import click

from imbue.mng import hookimpl
from imbue.mng_tutor.cli import tutor


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the tutor command with mng."""
    return [tutor]
