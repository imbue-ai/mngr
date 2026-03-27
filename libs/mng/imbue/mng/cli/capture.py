import sys
from typing import Any

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mng.cli.agent_utils import find_agent_for_command
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.config.data_types import CommonCliOptions


class CaptureCliOptions(CommonCliOptions):
    """Options passed from the CLI to the capture command."""

    agent: str | None
    start: bool
    full: bool


@click.command()
@click.argument("agent", default=None, required=False)
@optgroup.group("General")
@optgroup.option(
    "--start/--no-start",
    default=True,
    show_default=True,
    help="Automatically start the host/agent if stopped",
)
@optgroup.option(
    "--full/--no-full",
    default=False,
    show_default=True,
    help="Capture the full scrollback buffer instead of just the visible pane",
)
@add_common_options
@click.pass_context
def capture(ctx: click.Context, **kwargs: Any) -> None:
    mng_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="capture",
        command_class=CaptureCliOptions,
    )

    result = find_agent_for_command(
        mng_ctx=mng_ctx,
        agent_identifier=opts.agent,
        command_usage="capture",
        host_filter=None,
        is_start_desired=opts.start,
    )
    if result is None:
        return

    agent, _host = result

    logger.debug("Capturing pane content for agent: {}", agent.name)
    content = agent.capture_pane_content(include_scrollback=opts.full)
    if content is None:
        logger.error("Failed to capture pane content for agent {}", agent.name)
        ctx.exit(1)
        return

    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


CommandHelpMetadata(
    key="capture",
    one_line_description="Capture and display an agent's tmux pane content",
    synopsis="mng capture [AGENT] [--full] [--start/--no-start]",
    description="""Captures the current tmux pane content for the specified agent and
prints it to stdout. Useful for debugging agent state without connecting
to the agent's terminal.

By default, captures only the visible pane content. Use --full to capture
the entire scrollback buffer.

If no agent is specified and running interactively, shows a selector.""",
    examples=(
        ("Capture visible pane content", "mng capture my-agent"),
        ("Capture full scrollback buffer", "mng capture my-agent --full"),
        ("Capture without auto-starting", "mng capture my-agent --no-start"),
    ),
    see_also=(
        ("connect", "Connect to an agent interactively"),
        ("exec", "Execute a shell command on an agent's host"),
    ),
).register()

add_pager_help_option(capture)
