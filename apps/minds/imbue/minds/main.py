import click

from imbue.minds.cli.forward import forward
from imbue.minds.cli.update import update
from imbue.minds.utils.logging import LogFormat
from imbue.minds.utils.logging import console_level_from_verbose_and_quiet
from imbue.minds.utils.logging import setup_logging


@click.group()
@click.option("-v", "--verbose", count=True, help="Increase verbosity; -v for DEBUG, -vv for TRACE")
@click.option("-q", "--quiet", is_flag=True, default=False, help="Suppress all console output")
@click.option(
    "--log-format",
    type=click.Choice(["text", "jsonl"], case_sensitive=False),
    default="text",
    help="Log output format (jsonl for machine-parseable structured events)",
)
@click.pass_context
def cli(ctx: click.Context, verbose: int, quiet: bool, log_format: str) -> None:
    """minds: run and manage your own persistent, specialized AI agents."""
    console_level = console_level_from_verbose_and_quiet(verbose, quiet)
    parsed_log_format = LogFormat(log_format.upper())
    command_name = ctx.invoked_subcommand or "unknown"
    setup_logging(console_level, log_format=parsed_log_format, command=command_name)
    ctx.ensure_object(dict)
    ctx.obj["console_level"] = console_level
    ctx.obj["log_format"] = parsed_log_format


cli.add_command(forward)
cli.add_command(update)
