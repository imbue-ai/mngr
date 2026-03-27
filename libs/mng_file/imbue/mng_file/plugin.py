from collections.abc import Sequence

import click

from imbue.mng import hookimpl
from imbue.mng_file.cli.commands import file_group


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the file command group with mng."""
    return [file_group]
