from typing import Any

import click
from click_option_group import optgroup

from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.config.data_types import CommonCliOptions
from imbue.mng.utils.cel_utils import compile_cel_filters
from imbue.mng_kanpan.tui import run_kanpan


class KanpanCliOptions(CommonCliOptions):
    """Options for the kanpan command."""

    include: tuple[str, ...]
    exclude: tuple[str, ...]
    project: tuple[str, ...]


@click.command()
@optgroup.group("Filtering")
@optgroup.option(
    "--include",
    multiple=True,
    help="Include agents matching CEL expression (repeatable)",
)
@optgroup.option(
    "--exclude",
    multiple=True,
    help="Exclude agents matching CEL expression (repeatable)",
)
@optgroup.option(
    "--project",
    multiple=True,
    help="Show only agents with this project label (repeatable)",
)
@add_common_options
@click.pass_context
def kanpan(ctx: click.Context, **kwargs: Any) -> None:
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="kanpan",
        command_class=KanpanCliOptions,
    )

    # Build include/exclude filter tuples from CLI options
    include_filters = list(opts.include)
    if opts.project:
        project_parts = [f'labels.project == "{p}"' for p in opts.project]
        include_filters.append(" || ".join(project_parts))
    exclude_filters = list(opts.exclude)

    include_tuple = tuple(include_filters)
    exclude_tuple = tuple(exclude_filters)

    # Fail fast on invalid CEL expressions before launching the TUI
    if include_tuple or exclude_tuple:
        compile_cel_filters(include_tuple, exclude_tuple)

    run_kanpan(mng_ctx, include_filters=include_tuple, exclude_filters=exclude_tuple)


CommandHelpMetadata(
    key="kanpan",
    one_line_description="TUI board showing agents grouped by lifecycle state with PR status",
    synopsis="mng kanpan [OPTIONS]",
    description="""Launches a terminal UI that displays all mng agents organized by their
lifecycle state (RUNNING, WAITING, STOPPED, DONE, REPLACED).

Each agent shows its name, current state, and associated GitHub PR information
including PR number, state (open/closed/merged), and CI check status.

The display auto-refreshes every 10 minutes. Press 'r' to refresh manually,
or 'q' to quit.

Supports CEL filtering via --include/--exclude and a --project convenience flag.

Requires the gh CLI to be installed and authenticated for GitHub PR information.""",
    examples=(
        ("Launch the kanpan board", "mng kanpan"),
        ("Show only agents for a specific project", "mng kanpan --project mng"),
        ("Show only running agents", "mng kanpan --include 'state == \"RUNNING\"'"),
    ),
    see_also=(("list", "List agents"),),
).register()

add_pager_help_option(kanpan)
