import sys
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mng.api.message import MessageResult
from imbue.mng.api.message import send_message_to_agents
from imbue.mng.cli.common_opts import CommonCliOptions
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.output_helpers import AbortError
from imbue.mng.cli.output_helpers import emit_event
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.errors import UserInputError
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import OutputFormat


class MessageCliOptions(CommonCliOptions):
    """Options passed from the CLI to the message command.

    This captures all the click parameters so we can pass them as a single object
    to helper functions instead of passing dozens of individual parameters.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.

    Note that this class VERY INTENTIONALLY DOES NOT use Field() decorators with descriptions, defaults, etc.
    For that information, see the click.option() and click.argument() decorators on the message() function itself.
    """

    agents: tuple[str, ...]
    agent_list: tuple[str, ...]
    all_agents: bool
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    stdin: bool
    message_content: str | None
    on_error: str
    start: bool


@click.command(name="message")
@click.argument("agents", nargs=-1, required=False)
@optgroup.group("Target Selection")
@optgroup.option(
    "--agent",
    "agent_list",
    multiple=True,
    help="Agent name or ID to send message to (can be specified multiple times)",
)
@optgroup.option(
    "-a",
    "--all",
    "--all-agents",
    "all_agents",
    is_flag=True,
    help="Send message to all agents",
)
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
    "--stdin",
    is_flag=True,
    help="Read agent and host IDs or names from stdin (one per line)",
)
@optgroup.option(
    "--start/--no-start",
    default=False,
    show_default=True,
    help="Automatically start offline hosts and stopped agents before sending",
)
@optgroup.group("Message Content")
@optgroup.option(
    "-m",
    "--message",
    "message_content",
    help="The message content to send",
)
@optgroup.group("Error Handling")
@optgroup.option(
    "--on-error",
    type=click.Choice(["abort", "continue"], case_sensitive=False),
    default="continue",
    help="What to do when errors occur: abort (stop immediately) or continue (keep going)",
)
@add_common_options
@click.pass_context
def message(ctx: click.Context, **kwargs) -> None:
    try:
        _message_impl(ctx, **kwargs)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _message_impl(ctx: click.Context, **kwargs) -> None:
    """Implementation of message command (extracted for exception handling)."""
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="message",
        command_class=MessageCliOptions,
    )

    # Build list of agent identifiers
    agent_identifiers = list(opts.agents) + list(opts.agent_list)

    # Handle stdin input for agent identifiers
    stdin_refs: list[str] = []
    if opts.stdin:
        stdin_refs = [line.strip() for line in sys.stdin if line.strip()]
        agent_identifiers.extend(stdin_refs)

    # Validate input: must have agents specified or use --all or use filters
    if not agent_identifiers and not opts.all_agents and not opts.include:
        raise UserInputError("Must specify at least one agent, use --all, or use --include filters")

    if agent_identifiers and opts.all_agents:
        raise UserInputError("Cannot specify both agent names and --all")

    # Get message content
    message_content = _get_message_content(opts.message_content, ctx)

    error_behavior = ErrorBehavior(opts.on_error.upper())

    # Build include filters from agent identifiers
    include_filters = list(opts.include)
    if agent_identifiers:
        # Create a CEL filter that matches any of the provided identifiers
        ref_filters = []
        for ref in agent_identifiers:
            ref_filter = f'(name == "{ref}" || id == "{ref}")'
            ref_filters.append(ref_filter)
        combined_filter = " || ".join(ref_filters)
        include_filters.append(combined_filter)

    # For JSONL format, use streaming callbacks
    if output_opts.output_format == OutputFormat.JSONL:
        result = send_message_to_agents(
            mng_ctx=mng_ctx,
            message_content=message_content,
            include_filters=tuple(include_filters),
            exclude_filters=opts.exclude,
            all_agents=opts.all_agents,
            error_behavior=error_behavior,
            is_start_desired=opts.start,
            on_success=lambda agent_name: _emit_jsonl_success(agent_name),
            on_error=lambda agent_name, error: _emit_jsonl_error(agent_name, error),
        )
        if result.failed_agents:
            ctx.exit(1)
        return

    # For other formats, collect all results first
    result = send_message_to_agents(
        mng_ctx=mng_ctx,
        message_content=message_content,
        include_filters=tuple(include_filters),
        exclude_filters=opts.exclude,
        all_agents=opts.all_agents,
        error_behavior=error_behavior,
        is_start_desired=opts.start,
    )

    _emit_output(result, output_opts)

    if result.failed_agents:
        if output_opts.output_format == OutputFormat.HUMAN:
            failed_names = " ".join(name for name, _error in result.failed_agents)
            write_human_line("Failed agents: {}", failed_names)
        ctx.exit(1)


def _get_message_content(message_option: str | None, ctx: click.Context) -> str:
    """Get the message content from option, stdin, or editor."""
    if message_option is not None:
        return message_option

    # Check if stdin has data (not a tty)
    if not sys.stdin.isatty():
        return sys.stdin.read()

    # Interactive mode: open editor
    message_from_editor = click.edit()
    if message_from_editor is None:
        raise UserInputError("No message provided (editor was closed without saving)")

    return message_from_editor


def _emit_jsonl_success(agent_name: str) -> None:
    """Emit a success event as a JSONL line."""
    emit_event(
        "message_sent",
        {"agent": agent_name, "message": "Message sent successfully"},
        OutputFormat.JSONL,
    )


def _emit_jsonl_error(agent_name: str, error: str) -> None:
    """Emit an error event as a JSONL line."""
    emit_event(
        "message_error",
        {"agent": agent_name, "error": error},
        OutputFormat.JSONL,
    )


def _emit_output(result: MessageResult, output_opts: OutputOptions) -> None:
    """Emit output based on the result and format."""
    match output_opts.output_format:
        case OutputFormat.HUMAN:
            _emit_human_output(result)
        case OutputFormat.JSON:
            _emit_json_output(result)
        case OutputFormat.JSONL:
            # JSONL is handled with streaming above, should not reach here
            raise AssertionError("JSONL should be handled with streaming")
        case _ as unreachable:
            assert_never(unreachable)


def _emit_human_output(result: MessageResult) -> None:
    """Emit human-readable output."""
    if result.successful_agents:
        for agent_name in result.successful_agents:
            write_human_line("Message sent to: {}", agent_name)

    if result.failed_agents:
        for agent_name, error in result.failed_agents:
            logger.error("Failed to send message to {}: {}", agent_name, error)

    if not result.successful_agents and not result.failed_agents:
        write_human_line("No agents found to send message to")
    elif result.successful_agents:
        write_human_line("Successfully sent message to {} agent(s)", len(result.successful_agents))
    else:
        # Only failed agents, no successful ones - failures already logged above
        write_human_line("Failed to send message to {} agent(s)", len(result.failed_agents))


def _emit_json_output(result: MessageResult) -> None:
    """Emit JSON output."""
    output_data = {
        "successful_agents": result.successful_agents,
        "failed_agents": [{"agent": name, "error": error} for name, error in result.failed_agents],
        "total_sent": len(result.successful_agents),
        "total_failed": len(result.failed_agents),
    }
    emit_final_json(output_data)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="message",
    one_line_description="Send a message to one or more agents",
    synopsis="mng [message|msg] [AGENTS...] [--agent <AGENT>] [--all] [-m <MESSAGE>]",
    description="""Agent IDs can be specified as positional arguments for convenience. The
message is sent to the agent's stdin.

If no message is specified with --message, reads from stdin (if not a tty)
or opens an editor (if interactive).""",
    aliases=("msg",),
    examples=(
        ("Send a message to an agent", 'mng message my-agent --message "Hello"'),
        ("Send to multiple agents", 'mng message agent1 agent2 --message "Hello to all"'),
        ("Send to all agents", 'mng message --all --message "Hello everyone"'),
        ("Pipe message from stdin", 'echo "Hello" | mng message my-agent'),
        ("Use --agent flag (repeatable)", 'mng message --agent my-agent --agent another-agent --message "Hello"'),
    ),
    see_also=(
        ("connect", "Connect to an agent interactively"),
        ("list", "List available agents"),
    ),
    additional_sections=(
        (
            "Related Documentation",
            """- [Multi-target Options](../generic/multi_target.md) - Behavior when some agents fail to receive the message""",
        ),
    ),
).register()

# Add pager-enabled help option to the message command
add_pager_help_option(message)
