from collections.abc import Sequence

import click

from imbue.mng import hookimpl
from imbue.mng_changeling_chat.cli import chat


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the chat command with mng."""
    return [chat]
