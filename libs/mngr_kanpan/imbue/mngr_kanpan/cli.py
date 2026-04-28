from typing import Any

import click

from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.filter_opts import AgentFilterCliOptions
from imbue.mngr.cli.filter_opts import add_agent_filter_options
from imbue.mngr.cli.filter_opts import build_agent_filter_cel
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.utils.cel_utils import compile_cel_filters
from imbue.mngr_kanpan.tui import run_kanpan


class KanpanCliOptions(AgentFilterCliOptions, CommonCliOptions):
    """Options for the kanpan command."""


@click.command()
@add_agent_filter_options
@add_common_options
@click.pass_context
def kanpan(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="kanpan",
        command_class=KanpanCliOptions,
    )

    include_tuple, exclude_tuple = build_agent_filter_cel(opts)

    # Fail fast on invalid CEL expressions before launching the TUI
    if include_tuple or exclude_tuple:
        compile_cel_filters(include_tuple, exclude_tuple)

    run_kanpan(mngr_ctx, include_filters=include_tuple, exclude_filters=exclude_tuple)


CommandHelpMetadata(
    key="kanpan",
    one_line_description="TUI board showing agents grouped by lifecycle state with PR status",
    synopsis="mngr kanpan [OPTIONS]",
    description="""Launches a terminal UI that displays all mngr agents organized by their
lifecycle state (RUNNING, WAITING, STOPPED, DONE, REPLACED, RUNNING_UNKNOWN_AGENT_TYPE).

Each agent shows its name, current state, and associated GitHub PR information
including PR number, state (open/closed/merged), and CI check status.

The display auto-refreshes every 10 minutes. Press 'r' to refresh manually,
or 'q' to quit.

Filtering shares the flag set used by `mngr list` (--include/--exclude/--running/
--stopped/--archived/--active/--local/--remote/--project/--label/--host-label).

Requires the gh CLI to be installed and authenticated for GitHub PR information.""",
    examples=(
        ("Launch the kanpan board", "mngr kanpan"),
        ("Show only agents for a specific project", "mngr kanpan --project mngr"),
        ("Show only running agents", "mngr kanpan --running"),
    ),
    see_also=(("list", "List agents"),),
).register()

add_pager_help_option(kanpan)
