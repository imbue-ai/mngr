from pathlib import Path
from typing import Any

import click
from loguru import logger

from imbue.mngr.api.discovery_events import run_discovery_stream
from imbue.mngr.api.observe import AgentObserver
from imbue.mngr.api.observe import acquire_observe_lock
from imbue.mngr.api.observe import get_default_events_base_dir
from imbue.mngr.api.observe import release_observe_lock
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.primitives import ErrorBehavior


class ObserveCliOptions(CommonCliOptions):
    """Options for the observe command."""

    events_dir: Path | None = None
    discovery_only: bool = False
    on_error: str = "abort"


@click.command(name="observe")
@click.option(
    "--events-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Base directory for event output files and lock. Defaults to MNGR_HOST_DIR (~/.mngr).",
)
@click.option(
    "--discovery-only",
    is_flag=True,
    help="Stream only discovery events as JSONL (hosts and agents discovered/destroyed). "
    "Outputs a full snapshot, then tails the event file for updates. "
    "Periodically re-polls to catch any missed changes. "
    "Does not start activity streams or emit agent state events.",
)
@click.option(
    "--on-error",
    type=click.Choice(["abort", "continue"], case_sensitive=False),
    default="abort",
    help="What to do when errors occur: abort (stop immediately) or continue (keep going)",
)
@add_common_options
@click.pass_context
def observe(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="observe",
        command_class=ObserveCliOptions,
        is_format_template_supported=False,
    )

    events_base_dir = opts.events_dir
    if events_base_dir is None:
        events_base_dir = get_default_events_base_dir(mngr_ctx.config)

    if opts.discovery_only:
        error_behavior = ErrorBehavior(opts.on_error.upper())
        run_discovery_stream(
            mngr_ctx=mngr_ctx,
            error_behavior=error_behavior,
        )
        return

    # Acquire an exclusive lock per output directory
    lock_fd = acquire_observe_lock(events_base_dir)
    try:
        logger.info("Starting agent observer writing to {} (Ctrl+C to stop)", events_base_dir)
        observer = AgentObserver(mngr_ctx=mngr_ctx, events_base_dir=events_base_dir)
        observer.run()
    finally:
        release_observe_lock(lock_fd)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="observe",
    one_line_description="Observe agent state changes across all hosts [experimental]",
    synopsis="mngr observe [--events-dir DIR] [--discovery-only]",
    arguments_description="",
    description="""Continuously monitors agent state across all hosts and writes
events to local JSONL files:

- <events-dir>/events/mngr/agents/events.jsonl: individual and full agent state snapshots
- <events-dir>/events/mngr/agent_states/events.jsonl: only when the lifecycle state field changes

The observer:
1. Loads base state from event history (if available) to detect state changes since last run
2. Runs host discovery to track which hosts are online
3. Streams activity events from each online host
4. When activity is detected, fetches and emits agent state for the affected host
5. Periodically (every 5 minutes) emits a full state snapshot of all agents

Only one instance per output directory can run at a time (enforced via file lock).
Use --events-dir to write events to a different directory, allowing multiple
observers to run simultaneously for different output locations.

With --discovery-only, only the host/agent discovery stream is emitted as JSONL
to stdout. This is useful for programmatically tracking which agents and hosts
exist without the full observe overhead.

Press Ctrl+C to stop.""",
    examples=(
        ("Start observing all agents", "mngr observe"),
        ("Write events to a custom directory", "mngr observe --events-dir /path/to/events"),
        ("Start in quiet mode", "mngr observe --quiet"),
        ("Stream only discovery events", "mngr observe --discovery-only"),
    ),
    see_also=(
        ("list", "List available agents"),
        ("events", "View events from an agent or host"),
    ),
).register()

# Add pager-enabled help option
add_pager_help_option(observe)
