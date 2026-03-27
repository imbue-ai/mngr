from pathlib import Path
from typing import Any

import click
from loguru import logger

from imbue.mng.api.observe import AgentObserver
from imbue.mng.api.observe import acquire_observe_lock
from imbue.mng.api.observe import get_default_events_base_dir
from imbue.mng.api.observe import release_observe_lock
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.config.data_types import CommonCliOptions


class ObserveCliOptions(CommonCliOptions):
    """Options for the observe command."""

    events_dir: Path | None = None


@click.command(name="observe")
@click.option(
    "--events-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Base directory for event output files and lock. Defaults to MNG_HOST_DIR (~/.mng).",
)
@add_common_options
@click.pass_context
def observe(ctx: click.Context, **kwargs: Any) -> None:
    mng_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="observe",
        command_class=ObserveCliOptions,
        is_format_template_supported=False,
    )

    events_base_dir = opts.events_dir
    if events_base_dir is None:
        events_base_dir = get_default_events_base_dir(mng_ctx.config)

    # Acquire an exclusive lock per output directory
    lock_fd = acquire_observe_lock(events_base_dir)
    try:
        logger.info("Starting agent observer writing to {} (Ctrl+C to stop)", events_base_dir)
        observer = AgentObserver(mng_ctx=mng_ctx, events_base_dir=events_base_dir)
        observer.run()
    finally:
        release_observe_lock(lock_fd)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="observe",
    one_line_description="Observe agent state changes across all hosts [experimental]",
    synopsis="mng observe [--events-dir DIR]",
    arguments_description="",
    description="""Continuously monitors agent state across all hosts and writes
events to local JSONL files:

- <events-dir>/events/mng/agents/events.jsonl: individual and full agent state snapshots
- <events-dir>/events/mng/agent_states/events.jsonl: only when the lifecycle state field changes

The observer:
1. Loads base state from event history (if available) to detect state changes since last run
2. Uses 'mng list --stream' to track which hosts are online
3. Streams activity events from each online host
4. When activity is detected, fetches and emits agent state for the affected host
5. Periodically (every 5 minutes) emits a full state snapshot of all agents

Only one instance per output directory can run at a time (enforced via file lock).
Use --events-dir to write events to a different directory, allowing multiple
observers to run simultaneously for different output locations.

Press Ctrl+C to stop.""",
    examples=(
        ("Start observing all agents", "mng observe"),
        ("Write events to a custom directory", "mng observe --events-dir /path/to/events"),
        ("Start in quiet mode", "mng observe --quiet"),
    ),
    see_also=(
        ("list", "List available agents"),
        ("events", "View events from an agent or host"),
    ),
).register()

# Add pager-enabled help option
add_pager_help_option(observe)
