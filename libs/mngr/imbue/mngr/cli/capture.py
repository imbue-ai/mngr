import sys
from collections.abc import Iterator
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger
from pydantic import Field

from imbue.mngr.api.find import ResolvedAgentAddress
from imbue.mngr.api.find import UnresolvedAgentAddress
from imbue.mngr.api.find import find_each_agent_by_address
from imbue.mngr.api.find import resolve_to_started_host_and_running_agent
from imbue.mngr.cli.address_params import AGENT_ADDRESS
from imbue.mngr.cli.agent_utils import find_agent_by_address_or_interactively
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import write_command_stdout_and_stderr
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.target_outcomes import TargetOutcome
from imbue.mngr.cli.target_outcomes import emit_target_outcomes
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.primitives import AgentAddress


class CaptureCliOptions(CommonCliOptions):
    """Options passed from the CLI to the capture command."""

    agents: tuple[AgentAddress, ...]
    start: bool
    full: bool
    window: str | None


class _TargetCapture(TargetOutcome):
    """One target's outcome in a multi-target capture: its pane content, or why there is none."""

    content: str | None = Field(description="The captured pane content, or None when the target failed")


@click.command()
@click.argument("agents", type=AGENT_ADDRESS, nargs=-1)
@optgroup.group("General")
@optgroup.option(
    "--start/--no-start",
    default=True,
    show_default=True,
    help="Automatically start the host and agent if offline/stopped",
)
@optgroup.option(
    "--full/--no-full",
    default=False,
    show_default=True,
    help="Capture the full scrollback buffer instead of just the visible pane",
)
@optgroup.option(
    "--window",
    "-w",
    default=None,
    help="tmux window (index or name) to capture, instead of the agent's primary window",
)
@add_common_options
@click.pass_context
def capture(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="capture",
        command_class=CaptureCliOptions,
    )

    if len(opts.agents) > 1:
        _capture_multiple_targets(ctx, opts, output_opts, mngr_ctx)
        return

    host_ref, agent_ref = find_agent_by_address_or_interactively(
        mngr_ctx=mngr_ctx,
        address=opts.agents[0] if opts.agents else None,
        host_filter=None,
    )
    agent, _host = resolve_to_started_host_and_running_agent(
        host_ref=host_ref,
        agent_ref=agent_ref,
        allow_auto_start=opts.start,
        mngr_ctx=mngr_ctx,
    )

    logger.debug("Capturing pane content for agent: {}", agent.name)
    content = agent.capture_pane_content(include_scrollback=opts.full, window=opts.window)
    if content is None:
        logger.error(_describe_capture_failure(agent, opts.window))
        ctx.exit(1)
        return

    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def _describe_capture_failure(agent: AgentInterface, window: str | None) -> str:
    if window is None:
        return f"Failed to capture pane content for agent {agent.name}"
    return f"Failed to capture pane content for agent {agent.name} window {window!r} (does it exist?)"


def _capture_multiple_targets(
    ctx: click.Context,
    opts: CaptureCliOptions,
    output_opts: OutputOptions,
    mngr_ctx: MngrContext,
) -> None:
    """Capture every target against one discovery pass, reporting each target's failure on its own."""
    is_any_failed = emit_target_outcomes(
        _capture_each_target(opts, mngr_ctx), output_opts.output_format, _write_human_capture_block
    )
    if is_any_failed:
        ctx.exit(1)


def _capture_each_target(opts: CaptureCliOptions, mngr_ctx: MngrContext) -> Iterator[_TargetCapture]:
    for resolution in find_each_agent_by_address(opts.agents, mngr_ctx):
        yield _capture_one_resolved_target(resolution, opts, mngr_ctx)


def _capture_one_resolved_target(
    resolution: ResolvedAgentAddress | UnresolvedAgentAddress,
    opts: CaptureCliOptions,
    mngr_ctx: MngrContext,
) -> _TargetCapture:
    target = str(resolution.address)
    match resolution:
        case UnresolvedAgentAddress(error=error):
            return _TargetCapture(target=target, agent_id=None, name=None, error=str(error), content=None)
        case ResolvedAgentAddress(host_ref=host_ref, agent_ref=agent_ref):
            pass
        case _ as unreachable:
            assert_never(unreachable)
    agent_id = str(agent_ref.agent_id)
    name = str(agent_ref.agent_name)
    try:
        agent, _host = resolve_to_started_host_and_running_agent(
            host_ref=host_ref,
            agent_ref=agent_ref,
            allow_auto_start=opts.start,
            mngr_ctx=mngr_ctx,
        )
    except MngrError as e:
        return _TargetCapture(target=target, agent_id=agent_id, name=name, error=str(e), content=None)
    logger.debug("Capturing pane content for agent: {}", agent.name)
    content = agent.capture_pane_content(include_scrollback=opts.full, window=opts.window)
    if content is None:
        return _TargetCapture(
            target=target,
            agent_id=agent_id,
            name=name,
            error=_describe_capture_failure(agent, opts.window),
            content=None,
        )
    return _TargetCapture(target=target, agent_id=agent_id, name=name, error=None, content=content)


def _write_human_capture_block(target_capture: _TargetCapture, is_first_block: bool) -> None:
    """Write one target's pane under a ``==> name <==`` header, or its failure to stderr."""
    if target_capture.content is None:
        logger.error("{}: {}", target_capture.target, target_capture.error)
        return
    if not is_first_block:
        write_human_line("")
    write_human_line("==> {} <==", target_capture.name)
    write_command_stdout_and_stderr(target_capture.content, "")


CommandHelpMetadata(
    key="capture",
    one_line_description="Capture and display one or more agents' tmux pane content",
    synopsis="mngr capture [AGENTS...] [--full] [--window WINDOW] [--start/--no-start]",
    arguments_description="- `AGENTS`: Name(s), ID(s), or address(es) of the agent(s) to capture",
    description="""Captures the current tmux pane content for the specified agent and
prints it to stdout. Useful for debugging agent state without connecting
to the agent's terminal.

By default, captures only the visible pane content. Use --full to capture
the entire scrollback buffer.

By default, captures the agent's primary window. Use --window to capture a
different tmux window in the agent's session, by index (e.g. 1) or name.

If no agent is specified and running interactively, shows a selector.

Several agents can be captured in one call, which resolves all of them in a
single discovery pass. A target that fails (one that does not exist, or a
stopped agent under --no-start) reports its own error and the rest are still
captured; the exit code is 1 if any target failed. With more than one target,
human output puts a '==> NAME <==' header before each agent's pane, and
--format json/jsonl emits one record per target with the fields target,
agent_id, name, error and content (json as an array, jsonl one record per
line, each written as soon as that target is done). A single target's output
is unchanged.""",
    examples=(
        ("Capture visible pane content", "mngr capture my-agent"),
        ("Capture full scrollback buffer", "mngr capture my-agent --full"),
        ("Capture a specific tmux window", "mngr capture my-agent --window 1"),
        ("Capture without auto-starting", "mngr capture my-agent --no-start"),
        ("Capture several agents as JSONL", "mngr capture agent-1 agent-2 --full --no-start --format jsonl"),
    ),
    see_also=(
        ("connect", "Connect to an agent interactively"),
        ("exec", "Execute a shell command on an agent's host"),
        ("multi_target", "Behavior when targeting multiple agents"),
    ),
).register()

add_pager_help_option(capture)
