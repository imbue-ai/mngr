from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr_pair.cli import pair


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the pair command with mngr."""
    return [pair]
