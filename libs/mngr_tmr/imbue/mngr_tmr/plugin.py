from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr_tmr.cli import tmr
from imbue.mngr_tmr.spec_cli import tmr_specs


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the tmr-family commands with mngr."""
    return [tmr, tmr_specs]
