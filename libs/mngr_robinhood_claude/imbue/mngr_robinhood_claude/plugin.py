from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr_robinhood_claude.cli import robinhood_claude


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the robinhood-claude command with mngr."""
    return [robinhood_claude]
