import sys
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mngr.api.exec import ExecResult
from imbue.mngr.api.exec import MultiExecResult
from imbue.mngr.api.exec import exec_command_on_agents
from imbue.mngr.cli.agent_addr import find_agents_by_addresses
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import emit_final_json
from imbue.mngr.cli.output_helpers import emit_format_template_lines
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.stdin_utils import expand_stdin_placeholder
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import OutputFormat


class ExecCliOptions(CommonCliOptions):
    """Options passed from the CLI to the exec command.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.
    """

    agents: tuple[str, ...]
    agent_list: tuple[str, ...]
    exec_all: bool
    command_arg: str
    user: str | None
    cwd: str | None
    timeout: float | None
    start: bool
    on_error: str


@click.command(name="exec")
@click.argument("agents", nargs=-1, required=False)
@click.argument("command_arg", metavar="COMMAND")
@optgroup.group("Target Selection")
@optgroup.option(
    "--agent",
    "agent_list",
    multiple=True,
    help="Agent name or ID to exec on (can be specified multiple times)",
)
@optgroup.option(
    "-a",
    "--all",
    "--all-agents",
    "exec_all",
    is_flag=True,
    help="Execute the command on all agents",
)
@optgroup.group("Execution")
@optgroup.option(
    "--user",
    default=None,
    help="User to run the command as",
)
@optgroup.option(
    "--cwd",
    default=None,
    help="Working directory for the command (default: agent's work_dir)",
)
@optgroup.option(
    "--timeout",
    type=float,
    default=None,
    help="Timeout in seconds for the command",
)
@optgroup.group("General")
@optgroup.option(
    "--start/--no-start",
    default=True,
    show_default=True,
    help="Automatically start the host/agent if stopped",
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
def exec_command(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _exec_impl(ctx, **kwargs)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _exec_impl(ctx: click.Context, **kwargs: Any) -> None:
    """Implementation of exec command (extracted for exception handling)."""
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="exec",
        command_class=ExecCliOptions,
        is_format_template_supported=True,
    )
    logger.debug("Started exec command")

    # Build list of agent identifiers
    agent_identifiers = expand_stdin_placeholder(opts.agents) + list(opts.agent_list)

    if not agent_identifiers and not opts.exec_all:
        raise UserInputError("Must specify at least one agent or use --all")

    if agent_identifiers and opts.exec_all:
        raise UserInputError("Cannot specify both agent names and --all")

    error_behavior = ErrorBehavior(opts.on_error.upper())

    # Resolve agent addresses (NAME@HOST.PROVIDER) to agent IDs for the API layer.
    # This ensures host/provider filtering works correctly for disambiguation.
    resolved_identifiers: list[str]
    if agent_identifiers:
        matches = find_agents_by_addresses(
            raw_identifiers=agent_identifiers,
            filter_all=False,
            target_state=None,
            mngr_ctx=mngr_ctx,
        )
        resolved_identifiers = [str(m.agent_id) for m in matches]
    else:
        resolved_identifiers = agent_identifiers

    # For JSONL format, use streaming callbacks
    if output_opts.output_format == OutputFormat.JSONL:
        result = exec_command_on_agents(
            mngr_ctx=mngr_ctx,
            agent_identifiers=resolved_identifiers,
            command=opts.command_arg,
            is_all=opts.exec_all,
            user=opts.user,
            cwd=opts.cwd,
            timeout_seconds=opts.timeout,
            is_start_desired=opts.start,
            error_behavior=error_behavior,
            on_success=lambda r: _emit_jsonl_exec_result(r),
            on_error=lambda agent_name, error: _emit_jsonl_error(agent_name, error),
        )
        if result.is_any_failure:
            ctx.exit(1)
        return

    # For other formats, collect all results first
    result = exec_command_on_agents(
        mngr_ctx=mngr_ctx,
        agent_identifiers=resolved_identifiers,
        command=opts.command_arg,
        is_all=opts.exec_all,
        user=opts.user,
        cwd=opts.cwd,
        timeout_seconds=opts.timeout,
        is_start_desired=opts.start,
        error_behavior=error_behavior,
    )

    _emit_output(result, output_opts)

    is_any_failure = result.failed_agents or any(not r.success for r in result.successful_results)
    if is_any_failure:
        ctx.exit(1)


def _emit_jsonl_exec_result(result: ExecResult) -> None:
    """Emit an exec result event as a JSONL line."""
    emit_event(
        "exec_result",
        {
            "agent": result.agent_name,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "success": result.success,
        },
        OutputFormat.JSONL,
    )


def _emit_jsonl_error(agent_name: str, error: str) -> None:
    """Emit an error event as a JSONL line."""
    emit_event(
        "exec_error",
        {"agent": agent_name, "error": error},
        OutputFormat.JSONL,
    )


def _emit_output(result: MultiExecResult, output_opts: OutputOptions) -> None:
    """Emit output based on the result and format."""
    if output_opts.format_template is not None:
        items: list[dict[str, str]] = []
        for r in result.successful_results:
            items.append(
                {
                    "agent": r.agent_name,
                    "stdout": r.stdout.rstrip("\n"),
                    "stderr": r.stderr.rstrip("\n"),
                    "success": str(r.success).lower(),
                }
            )
        for agent_name, error in result.failed_agents:
            items.append(
                {
                    "agent": agent_name,
                    "stdout": "",
                    "stderr": error,
                    "success": "false",
                }
            )
        emit_format_template_lines(output_opts.format_template, items)
        return
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


def _emit_human_output(result: MultiExecResult) -> None:
    """Emit human-readable output for multi-agent exec results."""
    for exec_result in result.successful_results:
        # Show agent name header when there are multiple results
        is_multi = len(result.successful_results) + len(result.failed_agents) > 1
        if is_multi:
            write_human_line("--- {} ---", exec_result.agent_name)

        if exec_result.stdout:
            sys.stdout.write(exec_result.stdout)
            if not exec_result.stdout.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()

        if exec_result.stderr:
            sys.stderr.write(exec_result.stderr)
            if not exec_result.stderr.endswith("\n"):
                sys.stderr.write("\n")
            sys.stderr.flush()

        if exec_result.success:
            write_human_line("Command succeeded on agent {}", exec_result.agent_name)
        else:
            logger.error("Command failed on agent {}", exec_result.agent_name)

    for agent_name, error in result.failed_agents:
        logger.error("Failed on agent {}: {}", agent_name, error)


def _emit_json_output(result: MultiExecResult) -> None:
    """Emit JSON output for multi-agent exec results."""
    output_data = {
        "results": [
            {
                "agent": r.agent_name,
                "stdout": r.stdout,
                "stderr": r.stderr,
                "success": r.success,
            }
            for r in result.successful_results
        ],
        "failed_agents": [{"agent": name, "error": error} for name, error in result.failed_agents],
        "total_executed": len(result.successful_results),
        "total_failed": len(result.failed_agents),
    }
    emit_final_json(output_data)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="exec",
    one_line_description="Execute a shell command on one or more agents' hosts",
    synopsis="mngr [exec|x] [AGENTS...|-] COMMAND [--agent <AGENT>] [--all] [--user <USER>] [--cwd <DIR>] [--timeout <SECONDS>] [--on-error <MODE>]",
    arguments_description=(
        "- `AGENTS`: Name(s) or ID(s) of the agent(s) whose host will run the command\n"
        "- `COMMAND`: Shell command to execute on the agent's host"
    ),
    description="""The command runs in each agent's work_dir by default. Use --cwd to override
the working directory.

The command's stdout is printed to stdout and stderr to stderr. The exit
code is 0 if all commands succeeded, 1 if any failed.

Supports custom format templates via --format. Available fields: agent, stdout, stderr, success.""",
    aliases=("x",),
    examples=(
        ("Run a command on an agent", 'mngr exec my-agent "echo hello"'),
        ("Run on multiple agents", 'mngr exec agent1 agent2 "echo hello"'),
        ("Run on all agents", 'mngr exec --all "echo hello"'),
        ("Run with a custom working directory", 'mngr exec my-agent "ls -la" --cwd /tmp'),
        ("Run as a different user", 'mngr exec my-agent "whoami" --user root'),
        ("Run with a timeout", 'mngr exec my-agent "sleep 100" --timeout 5'),
        ("Use --agent flag (repeatable)", 'mngr exec --agent my-agent --agent another-agent "echo hello"'),
        ("Custom format template output", "mngr exec --all \"hostname\" --format '{agent}\\t{stdout}'"),
    ),
    see_also=(
        ("connect", "Connect to an agent interactively"),
        ("message", "Send a message to an agent"),
        ("list", "List available agents"),
    ),
    additional_sections=(
        (
            "Related Documentation",
            """- [Multi-target Options](../generic/multi_target.md) - Behavior when targeting multiple agents""",
        ),
    ),
).register()

# Add pager-enabled help option to the exec command
add_pager_help_option(exec_command)
