from typing import Any
from typing import assert_never

import click

from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.filter_opts import AgentFilterCliOptions
from imbue.mngr.cli.filter_opts import add_agent_filter_options
from imbue.mngr.cli.filter_opts import build_agent_filter_cel
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_kanpan.data_types import KanpanPluginConfig
from imbue.mngr_kanpan.fetcher import collect_data_sources
from imbue.mngr_kanpan.fetcher import fetch_board_snapshot
from imbue.mngr_kanpan.fetcher import load_field_cache
from imbue.mngr_kanpan.serialize import board_snapshot_to_json
from imbue.mngr_kanpan.serialize import board_snapshot_to_jsonl_entries
from imbue.mngr_kanpan.tui import resolve_board_layout
from imbue.mngr_kanpan.tui import run_kanpan


class KanpanCliOptions(AgentFilterCliOptions, CommonCliOptions):
    """Options for the kanpan command."""


def _emit_board_data(
    mngr_ctx: MngrContext,
    output_format: OutputFormat,
    include_filters: tuple[str, ...],
    exclude_filters: tuple[str, ...],
) -> None:
    """Fetch a single board snapshot and emit it as JSON or JSONL.

    A read-only one-shot: it loads the on-disk field cache (so data sources reuse
    the same cached values the TUI would on launch) but does not write it back.
    """
    plugin_config = mngr_ctx.get_plugin_config("kanpan", KanpanPluginConfig)
    data_sources = collect_data_sources(mngr_ctx)
    cached_fields = load_field_cache(mngr_ctx, data_sources)

    fetch_result = fetch_board_snapshot(
        mngr_ctx,
        data_sources,
        cached_fields,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
    )
    snapshot = fetch_result.snapshot
    columns, section_order = resolve_board_layout(data_sources, plugin_config)

    match output_format:
        case OutputFormat.JSON:
            write_json_line(board_snapshot_to_json(snapshot, columns, section_order))
        case OutputFormat.JSONL:
            for entry_data in board_snapshot_to_jsonl_entries(snapshot, section_order):
                write_json_line(entry_data)
            for error in snapshot.errors:
                write_json_line({"event": "error", "message": error})
        case OutputFormat.HUMAN:
            raise AssertionError("HUMAN format is handled by the TUI, not _emit_board_data")
        case _ as unreachable:
            assert_never(unreachable)


@click.command()
@add_agent_filter_options
@add_common_options
@click.pass_context
def kanpan(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="kanpan",
        command_class=KanpanCliOptions,
    )

    include_tuple, exclude_tuple = build_agent_filter_cel(
        opts, mngr_ctx.concurrency_group, project_root=mngr_ctx.project_root
    )

    if output_opts.output_format is not OutputFormat.HUMAN:
        _emit_board_data(mngr_ctx, output_opts.output_format, include_tuple, exclude_tuple)
        return

    run_kanpan(mngr_ctx, include_filters=include_tuple, exclude_filters=exclude_tuple)


CommandHelpMetadata(
    key="kanpan",
    one_line_description="TUI board showing agents grouped by lifecycle state with PR status",
    synopsis="mngr kanpan [--include CEL] [--exclude CEL] [--running] [--stopped] [--archived] [--active] [--local] [--remote] [--project PROJECT]",
    description="""Launches a terminal UI that displays all mngr agents organized by their
lifecycle state (RUNNING, WAITING, STOPPED, DONE, REPLACED, RUNNING_UNKNOWN_AGENT_TYPE, UNKNOWN).

Each agent shows its name, current state, and associated GitHub PR information
including PR number, state (open/closed/merged), and CI check status.

The display auto-refreshes every 10 minutes. Press 'r' to refresh manually,
or 'q' to quit.

Pass `--format json` (or `jsonl`) to skip the TUI and print a single board
snapshot for programmatic use instead. JSON emits one object with the ordered
columns, agents grouped into sections, and any fetch errors; JSONL emits one
agent record per line (in board order) followed by any error lines. Each agent
carries both the pre-rendered cells and the structured field values (PR number,
CI status, etc.).

Supports CEL filtering via --include/--exclude plus alias flags (--running,
--stopped, --archived, --active, --local, --remote, --project, --label,
--host-label). See `mngr list --help` for the full filter reference; the same
flags work identically here.

Requires the gh CLI to be installed and authenticated for GitHub PR information.""",
    examples=(
        ("Launch the kanpan board", "mngr kanpan"),
        ("Show only agents for a specific project", "mngr kanpan --project mngr"),
        ("Show only running agents", "mngr kanpan --running"),
        ("Show stopped agents with a specific label", "mngr kanpan --stopped --label env=prod"),
        ("Print the board as JSON for scripting", "mngr kanpan --format json"),
    ),
    see_also=(("list#filtering", "List agents (see its Filtering section for the full flag reference)"),),
).register()

add_pager_help_option(kanpan)
