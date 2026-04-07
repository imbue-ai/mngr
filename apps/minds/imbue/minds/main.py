from pathlib import Path

import click

from imbue.minds.cli.forward import forward
from imbue.minds.cli.update import update
from imbue.minds.primitives import OutputFormat
from imbue.minds.utils.logging import console_level_from_verbose_and_quiet
from imbue.minds.utils.logging import setup_logging


@click.group()
@click.option("-v", "--verbose", count=True, help="Increase verbosity; -v for DEBUG, -vv for TRACE")
@click.option("-q", "--quiet", is_flag=True, default=False, help="Suppress all console output")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["human", "json", "jsonl"], case_sensitive=False),
    default="human",
    help="Output format for results on stdout",
)
@click.option(
    "--log-file",
    type=click.Path(),
    default=None,
    help="Path to a JSONL log file for persistent logging",
)
@click.pass_context
def cli(ctx: click.Context, verbose: int, quiet: bool, output_format: str, log_file: str | None) -> None:
    """minds: run and manage your own persistent, specialized AI agents."""
    console_level = console_level_from_verbose_and_quiet(verbose, quiet)
    command_name = ctx.invoked_subcommand or "unknown"
    log_file_path = Path(log_file) if log_file else None
    setup_logging(console_level, command=command_name, log_file=log_file_path)
    ctx.ensure_object(dict)
    ctx.obj["console_level"] = console_level
    ctx.obj["output_format"] = OutputFormat(output_format.upper())


cli.add_command(forward)
cli.add_command(update)
