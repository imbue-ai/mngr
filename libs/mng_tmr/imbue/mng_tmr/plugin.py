from collections.abc import Sequence

import click

from imbue.mng import hookimpl
from imbue.mng_tmr.cli import tmr


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the tmr command with mng."""
    return [tmr]
