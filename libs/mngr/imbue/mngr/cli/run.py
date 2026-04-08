from typing import Any

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.headless_runner import get_local_host
from imbue.mngr.cli.headless_runner import headless_agent_output
from imbue.mngr.cli.headless_runner import stream_or_accumulate_response
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString


class RunCliOptions(CommonCliOptions):
    """Options passed from the CLI to the run command."""

    agent_type: str
    command: str | None
    agent_args: tuple[str, ...]


class _RunCommand(click.Command):
    """Custom Command subclass that correctly handles -- for agent arg passthrough.

    Click's default behavior can fill unfilled optional positional arguments from
    args after --. This override strips everything after -- before Click's parser
    runs, then appends the stripped args to ``agent_args`` after parsing completes.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if "--" in args:
            idx = args.index("--")
            after_dash = tuple(args[idx + 1 :])
            args = args[:idx]
        else:
            after_dash = ()
        result = super().parse_args(ctx, args)
        ctx.params["agent_args"] = ctx.params.get("agent_args", ()) + after_dash
        return result


@click.command(name="run", cls=_RunCommand)
@click.argument("agent_type")
@click.argument("agent_args", nargs=-1, type=click.UNPROCESSED)
@optgroup.group("Execution")
@optgroup.option(
    "--command",
    "-c",
    default=None,
    help="Shell command for the agent to run (used by headless_command agent type)",
)
@add_common_options
@click.pass_context
def run_command(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _run_impl(ctx, **kwargs)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _run_impl(ctx: click.Context, **kwargs: Any) -> None:
    """Implementation of run command (extracted for exception handling)."""
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="run",
        command_class=RunCliOptions,
    )
    logger.debug("Started run command")

    host = get_local_host(mngr_ctx)

    command_override = CommandString(opts.command) if opts.command else None

    with headless_agent_output(
        host=host,
        mngr_ctx=mngr_ctx,
        agent_type=AgentTypeName(opts.agent_type),
        agent_args=opts.agent_args,
        command=command_override,
        label_options=AgentLabelOptions(labels={"internal": "run"}),
        name=AgentName("run"),
    ) as agent:
        chunks = agent.stream_output()
        stream_or_accumulate_response(
            chunks=chunks,
            output_format=output_opts.output_format,
        )


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="run",
    one_line_description="Run a headless agent and stream its output",
    synopsis="mngr run AGENT_TYPE [-c COMMAND] [-- AGENT_ARGS...]",
    arguments_description=(
        "AGENT_TYPE is the headless agent type to run (e.g. headless_command, headless_claude).\n"
        "AGENT_ARGS are additional arguments passed through to the agent after --."
    ),
    description="""Run a headless agent of any type that supports streaming output,
stream the output to stdout, and destroy the agent when done.

Use --command (-c) to specify the shell command for headless_command agents.
Use -- to pass additional arguments directly to the agent.""",
    examples=(
        ("Run a shell command", 'mngr run headless_command -c "echo hello world"'),
        ("Run headless claude", 'mngr run headless_claude -- "what is 2+2"'),
    ),
    see_also=(
        ("ask", "Ask mngr for help (headless_claude with built-in system prompt)"),
        ("create", "Create a persistent agent"),
        ("exec", "Execute a command on existing agents"),
    ),
).register()

# Add pager-enabled help option to the run command
add_pager_help_option(run_command)
