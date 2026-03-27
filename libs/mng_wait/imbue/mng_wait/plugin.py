from collections.abc import Sequence

import click

from imbue.mng import hookimpl
from imbue.mng_wait.cli import wait


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the wait command with mng."""
    return [wait]
