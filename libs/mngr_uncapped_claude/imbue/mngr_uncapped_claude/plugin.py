from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr_uncapped_claude.cli import uncapped_claude


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the uncapped-claude command with mngr."""
    return [uncapped_claude]
