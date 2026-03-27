import click

from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.stop import stop as stop_cmd


@click.command(
    context_settings={"ignore_unknown_options": True},
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def archive(ctx: click.Context, args: tuple[str, ...]) -> None:
    """Stop and archive agents (sets the 'archived_at' label).

    This is a shorthand for 'mngr stop --archive'. All arguments are
    passed through to the stop command with --archive injected.
    """
    stop_args = ["--archive", *args]
    stop_ctx = stop_cmd.make_context("archive", list(stop_args), parent=ctx)
    with stop_ctx:
        stop_cmd.invoke(stop_ctx)


CommandHelpMetadata(
    key="archive",
    one_line_description="Stop and archive agents",
    synopsis="mngr archive [AGENTS...] [--agent <AGENT>] [--all] [--dry-run] [stop-options...]",
    arguments_description="- `AGENTS`: Agent name(s) or ID(s) to archive. All arguments are passed through to the stop command.",
    description="""Shorthand for 'mngr stop --archive'. Stops the specified agents and sets
an 'archived_at' label with the current UTC timestamp on each one.

Archived agents remain in 'mngr list' output but can be filtered out
using label-based filtering. Their state is preserved (not destroyed),
so they can be restarted later if needed.

All options from the stop command are supported.""",
    examples=(
        ("Archive a single agent", "mngr archive my-agent"),
        ("Archive multiple agents", "mngr archive agent1 agent2"),
        ("Archive all running agents", "mngr archive --all"),
        ("Preview what would be archived", "mngr archive --all --dry-run"),
    ),
    see_also=(
        ("stop", "Stop agents without archiving"),
        ("label", "Set arbitrary labels on agents"),
        ("list", "List agents (use labels to filter archived agents)"),
        ("start", "Restart archived agents"),
    ),
).register()

add_pager_help_option(archive)
