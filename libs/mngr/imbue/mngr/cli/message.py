import sys
from pathlib import Path
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mngr.api.find import find_all_agents
from imbue.mngr.api.message import MessageResult
from imbue.mngr.api.message import send_message_to_agents
from imbue.mngr.cli.address_params import AGENT_ADDRESS
from imbue.mngr.cli.address_params import parse_agent_addresses_or_raise
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.cli.stdin_utils import STDIN_PLACEHOLDER
from imbue.mngr.cli.stdin_utils import expand_stdin_placeholder
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import AgentNotFoundError
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import OutputFormat


class MessageCliOptions(CommonCliOptions):
    """Options passed from the CLI to the message command.

    This captures all the click parameters so we can pass them as a single object
    to helper functions instead of passing dozens of individual parameters.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.

    Note that this class VERY INTENTIONALLY DOES NOT use Field() decorators with descriptions, defaults, etc.
    For that information, see the click.option() and click.argument() decorators on the message() function itself.
    """

    agents: tuple[str, ...]
    agent_list: tuple[AgentAddress, ...]
    message_content: str | None
    message_file: str | None
    on_error: str
    start: bool


@click.command(name="message")
@click.argument("agents", nargs=-1, required=False)
@optgroup.group("Target Selection")
@optgroup.option(
    "--agent",
    "agent_list",
    type=AGENT_ADDRESS,
    multiple=True,
    help="Agent address (NAME[@HOST[.PROVIDER]]) to send message to (can be specified multiple times)",
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
@optgroup.option(
    "--message-file",
    type=click.Path(exists=True),
    help="File containing the message content to send",
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
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="message",
        command_class=MessageCliOptions,
    )

    # Validate that --message and --message-file are not both provided
    if opts.message_content is not None and opts.message_file is not None:
        raise UserInputError("Cannot provide both --message and --message-file")

    # Build list of agent addresses
    stdin_consumed = STDIN_PLACEHOLDER in opts.agents
    agent_addresses: list[AgentAddress] = parse_agent_addresses_or_raise(expand_stdin_placeholder(opts.agents)) + list(
        opts.agent_list
    )

    # Validate input: must have agents specified.
    if not agent_addresses:
        if not stdin_consumed:
            raise UserInputError(
                "Must specify at least one agent (use '-' to read agent ids from stdin, "
                "e.g. `mngr list --ids | mngr message -`)"
            )
        return

    # Read message from file if --message-file is provided
    resolved_message_content = opts.message_content
    if opts.message_file is not None:
        resolved_message_content = Path(opts.message_file).read_text()

    # Get message content
    message_content = _get_message_content(
        resolved_message_content, ctx, is_interactive=mngr_ctx.is_interactive, stdin_consumed=stdin_consumed
    )

    error_behavior = ErrorBehavior(opts.on_error.upper())

    # Resolve addresses to live agents. find_all_agents narrows discovery to the
    # providers named by the addresses (full scan only when an address omits
    # its provider) and raises AgentNotFoundError if any address has no match.
    # Treat that as "no agents found": preserve historical exit-0 behavior in
    # CONTINUE mode, surface it in ABORT mode.
    try:
        matches = find_all_agents(
            addresses=agent_addresses,
            filter_all=False,
            target_state=None,
            mngr_ctx=mngr_ctx,
        )
    except AgentNotFoundError:
        if error_behavior == ErrorBehavior.ABORT:
            raise
        matches = []

    # For JSONL format, use streaming callbacks
    if output_opts.output_format == OutputFormat.JSONL:
        result = send_message_to_agents(
            mngr_ctx=mngr_ctx,
            message_content=message_content,
            agents_to_message=matches,
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
        mngr_ctx=mngr_ctx,
        message_content=message_content,
        agents_to_message=matches,
        error_behavior=error_behavior,
        is_start_desired=opts.start,
    )

    _emit_output(result, output_opts)

    if result.failed_agents:
        if output_opts.output_format == OutputFormat.HUMAN:
            failed_names = " ".join(name for name, _error in result.failed_agents)
            write_human_line("Failed agents: {}", failed_names)
        ctx.exit(1)


def _get_message_content(
    message_option: str | None,
    ctx: click.Context,
    is_interactive: bool,
    stdin_consumed: bool = False,
) -> str:
    """Get the message content from option, stdin, or editor."""
    if message_option is not None:
        return message_option

    # If stdin was consumed by '-' for agent names, we can't also read it for message content
    if stdin_consumed:
        raise UserInputError(
            "When using '-' for agent names, message content must be provided via --message or --message-file"
        )

    # Check if stdin has piped data (not a tty)
    if not sys.stdin.isatty():
        return sys.stdin.read()

    # In headless mode, we cannot open an editor
    if not is_interactive:
        raise UserInputError(
            "No message provided and running in headless mode (use --message or --message-file to provide one)"
        )

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
    write_json_line(output_data)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="message",
    one_line_description="Send a message to one or more agents",
    synopsis="mngr [message|msg] [AGENTS...|-] [--agent <AGENT>] [-m <MESSAGE>] [--message-file <FILE>] [--[no-]start] [--on-error <MODE>]",
    description="""Agent IDs can be specified as positional arguments for convenience. The
message is sent to the agent's stdin.

If no message is specified with --message or --message-file, reads from stdin
(if not a tty) or opens an editor (if interactive).

Use '-' in place of agent names to read them from stdin, one per line.""",
    aliases=("msg",),
    examples=(
        ("Send a message to an agent", 'mngr message my-agent --message "Hello"'),
        ("Send to multiple agents", 'mngr message agent1 agent2 --message "Hello to all"'),
        ("Send to all agents via stdin", "mngr list --ids | mngr message - --message 'Hello everyone'"),
        ("Send message from a file", "mngr message my-agent --message-file prompt.txt"),
        ("Pipe message from stdin", 'echo "Hello" | mngr message my-agent'),
        ("Use --agent flag (repeatable)", 'mngr message --agent my-agent --agent another-agent --message "Hello"'),
    ),
    see_also=(
        ("connect", "Connect to an agent interactively"),
        ("list", "List available agents"),
        ("multi_target", "Behavior when some agents fail to receive the message"),
    ),
).register()

# Add pager-enabled help option to the message command
add_pager_help_option(message)
