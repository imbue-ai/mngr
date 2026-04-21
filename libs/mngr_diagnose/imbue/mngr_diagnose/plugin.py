from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr_diagnose.cli import diagnose


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the diagnose command with mngr."""
    return [diagnose]
