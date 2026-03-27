from __future__ import annotations

from collections.abc import Sequence

import click

from imbue.mngr import hookimpl


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register mind supporting service commands with mngr."""
    from imbue.mngr_mind.cli import get_all_commands

    return get_all_commands()
