from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr_robinhood.cli import robinhood


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the robinhood command with mngr."""
    return [robinhood]
