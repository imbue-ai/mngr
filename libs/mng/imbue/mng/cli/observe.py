from typing import Any

import click
from loguru import logger

from imbue.mng.api.observe import AgentObserver
from imbue.mng.api.observe import acquire_observe_lock
from imbue.mng.api.observe import release_observe_lock
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.config.data_types import CommonCliOptions


class ObserveCliOptions(CommonCliOptions):
    """Options for the observe command."""

    ...


@click.command(name="observe")
@add_common_options
@click.pass_context
def observe(ctx: click.Context, **kwargs: Any) -> None:
    mng_ctx, _output_opts, _opts = setup_command_context(
        ctx=ctx,
        command_name="observe",
        command_class=ObserveCliOptions,
        is_format_template_supported=False,
    )

    # Acquire an exclusive lock to prevent multiple instances
    lock_fd = acquire_observe_lock(mng_ctx.config)
    try:
        logger.info("Starting agent observer (Ctrl+C to stop)")
        observer = AgentObserver(mng_ctx=mng_ctx)
        observer.run()
    finally:
        release_observe_lock(lock_fd)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="observe",
    one_line_description="Observe agent state changes across all hosts [experimental]",
    synopsis="mng observe",
    arguments_description="",
    description="""Continuously monitors agent state across all hosts and writes
events to local JSONL files:

- events/mng/agents/events.jsonl: individual and full agent state snapshots
- events/mng/agent_states/events.jsonl: only when the lifecycle state field changes

The observer:
1. Loads base state from event history (if available) to detect state changes since last run
2. Uses 'mng list --stream' to track which hosts are online
3. Streams activity events from each online host
4. When activity is detected, fetches and emits agent state for the affected host
5. Periodically (every 5 minutes) emits a full state snapshot of all agents

Only one instance of 'mng observe' can run at a time (enforced via file lock).

Press Ctrl+C to stop.""",
    examples=(
        ("Start observing all agents", "mng observe"),
        ("Start in quiet mode", "mng observe --quiet"),
    ),
    see_also=(
        ("list", "List available agents"),
        ("events", "View events from an agent or host"),
    ),
).register()

# Add pager-enabled help option
add_pager_help_option(observe)
