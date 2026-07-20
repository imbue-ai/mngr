from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr_tmr.cli import tmr
from imbue.mngr_tmr.cli import tmr_tasks


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the tmr and tmr-tasks commands with mngr."""
    return [tmr, tmr_tasks]
