from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr_mind_chat.cli import chat


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the chat command with mngr."""
    return [chat]
