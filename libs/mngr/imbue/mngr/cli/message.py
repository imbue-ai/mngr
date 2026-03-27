import sys
from pathlib import Path
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mngr.api.message import MessageResult
from imbue.mngr.api.message import send_message_to_agents
from imbue.mngr.cli.agent_addr import parse_identifier_as_address
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import emit_final_json
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.stdin_utils import STDIN_PLACEHOLDER
from imbue.mngr.cli.stdin_utils import expand_stdin_placeholder
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import UserInputError
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
    agent_list: tuple[str, ...]
    all_agents: bool
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    message_content: str | None
    message_file: str | None
    provider: tuple[str, ...]
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
@optgroup.option(
    "--provider",
    multiple=True,
    help="Message only agents using specified provider (repeatable)",
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

    # Build list of agent identifiers
    stdin_consumed = STDIN_PLACEHOLDER in opts.agents
    agent_identifiers = expand_stdin_placeholder(opts.agents) + list(opts.agent_list)

    # Validate input: must have agents specified or use --all or use filters
    if not agent_identifiers and not opts.all_agents and not opts.include:
        raise UserInputError("Must specify at least one agent, use --all, or use --include filters")

    if agent_identifiers and opts.all_agents:
        raise UserInputError("Cannot specify both agent names and --all")

    # Read message from file if --message-file is provided
    resolved_message_content = opts.message_content
    if opts.message_file is not None:
        resolved_message_content = Path(opts.message_file).read_text()

    # Get message content
    message_content = _get_message_content(
        resolved_message_content, ctx, is_interactive=mngr_ctx.is_interactive, stdin_consumed=stdin_consumed
    )

    error_behavior = ErrorBehavior(opts.on_error.upper())

    # Build include filters from agent identifiers, parsing addresses
    include_filters = list(opts.include)
    if agent_identifiers:
        # Create a CEL filter that matches any of the provided identifiers.
        # Parse agent addresses to extract the name/ID part and host/provider constraints.
        ref_filters = []
        for ref in agent_identifiers:
            plain_id, address = parse_identifier_as_address(ref)
            ref_filter = f'(name == "{plain_id}" || id == "{plain_id}")'
            if address.host_name is not None:
                ref_filter += f' && host.name == "{address.host_name}"'
            if address.provider_name is not None:
                ref_filter += f' && host.provider == "{address.provider_name}"'
            ref_filters.append(f"({ref_filter})")
        combined_filter = " || ".join(ref_filters)
        include_filters.append(combined_filter)

    # For JSONL format, use streaming callbacks
    if output_opts.output_format == OutputFormat.JSONL:
        result = send_message_to_agents(
            mngr_ctx=mngr_ctx,
            message_content=message_content,
            include_filters=tuple(include_filters),
            exclude_filters=opts.exclude,
            all_agents=opts.all_agents,
            error_behavior=error_behavior,
            is_start_desired=opts.start,
            on_success=lambda agent_name: _emit_jsonl_success(agent_name),
            on_error=lambda agent_name, error: _emit_jsonl_error(agent_name, error),
            provider_names=opts.provider,
        )
        if result.failed_agents:
            ctx.exit(1)
        return

    # For other formats, collect all results first
    result = send_message_to_agents(
        mngr_ctx=mngr_ctx,
        message_content=message_content,
        include_filters=tuple(include_filters),
        exclude_filters=opts.exclude,
        all_agents=opts.all_agents,
        error_behavior=error_behavior,
        is_start_desired=opts.start,
        provider_names=opts.provider,
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
    emit_final_json(output_data)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="message",
    one_line_description="Send a message to one or more agents",
    synopsis="mngr [message|msg] [AGENTS...|-] [--agent <AGENT>] [--all] [-m <MESSAGE>] [--message-file <FILE>]",
    description="""Agent IDs can be specified as positional arguments for convenience. The
message is sent to the agent's stdin.

If no message is specified with --message or --message-file, reads from stdin
(if not a tty) or opens an editor (if interactive).""",
    aliases=("msg",),
    examples=(
        ("Send a message to an agent", 'mngr message my-agent --message "Hello"'),
        ("Send to multiple agents", 'mngr message agent1 agent2 --message "Hello to all"'),
        ("Send to all agents", 'mngr message --all --message "Hello everyone"'),
        ("Send message from a file", "mngr message my-agent --message-file prompt.txt"),
        ("Pipe message from stdin", 'echo "Hello" | mngr message my-agent'),
        ("Use --agent flag (repeatable)", 'mngr message --agent my-agent --agent another-agent --message "Hello"'),
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
