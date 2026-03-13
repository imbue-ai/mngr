import click

from imbue.minds.cli.forward import forward
from imbue.minds.utils.logging import console_level_from_verbose_and_quiet
from imbue.minds.utils.logging import setup_logging


@click.group()
@click.option("-v", "--verbose", count=True, help="Increase verbosity; -v for DEBUG, -vv for TRACE")
@click.option("-q", "--quiet", is_flag=True, default=False, help="Suppress all console output")
@click.pass_context
def cli(ctx: click.Context, verbose: int, quiet: bool) -> None:
    """minds: run and manage your own persistent, specialized AI agents."""
    console_level = console_level_from_verbose_and_quiet(verbose, quiet)
    setup_logging(console_level)
    ctx.ensure_object(dict)
    ctx.obj["console_level"] = console_level


cli.add_command(forward)
