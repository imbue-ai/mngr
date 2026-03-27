from collections.abc import Sequence

import click

from imbue.mng import hookimpl
from imbue.mng_pair.cli import pair


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the pair command with mng."""
    return [pair]
