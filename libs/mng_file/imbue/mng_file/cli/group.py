from typing import Any

import click


@click.group(name="file")
@click.pass_context
def file_group(ctx: click.Context, **kwargs: Any) -> None:
    """Read, write, and list files on agents and hosts."""
