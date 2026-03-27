from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr_file.cli.commands import file_group


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the file command group with mngr."""
    return [file_group]
