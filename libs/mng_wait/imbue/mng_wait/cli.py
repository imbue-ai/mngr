import io
import sys
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.output_helpers import emit_event
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.cli.output_helpers import emit_info
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.config.data_types import CommonCliOptions
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.primitives import OutputFormat
from imbue.mng.utils.duration import parse_duration_to_seconds
from imbue.mng_wait.api import poll_target_state
from imbue.mng_wait.api import resolve_wait_target
from imbue.mng_wait.api import wait_for_state
from imbue.mng_wait.data_types import StateChange
from imbue.mng_wait.data_types import WaitResult
from imbue.mng_wait.data_types import check_state_match
from imbue.mng_wait.data_types import compute_default_target_states
from imbue.mng_wait.data_types import describe_combined_state
from imbue.mng_wait.data_types import validate_state_strings
from imbue.mng_wait.primitives import ALL_VALID_STATE_STRINGS
from imbue.mng_wait.primitives import EXIT_CODE_ERROR
from imbue.mng_wait.primitives import EXIT_CODE_SUCCESS
from imbue.mng_wait.primitives import EXIT_CODE_TIMEOUT


class WaitCliOptions(CommonCliOptions):
    """CLI options for the wait command."""

    target: str | None
    states: tuple[str, ...]
    state: tuple[str, ...]
    timeout: str | None
    interval: str


def _read_target_from_stdin(
    stdin: io.TextIOBase | None = None,
) -> str:
    """Read a target identifier from stdin (one line)."""
    stream = stdin if stdin is not None else sys.stdin
    if hasattr(stream, "isatty") and stream.isatty():
        write_human_line("Waiting for target on stdin...")
    line = stream.readline().strip()
    if not line:
        raise click.UsageError("No target provided on stdin")
    return line


def _emit_state_change(change: StateChange, output_format: OutputFormat) -> None:
    """Emit a state change event in the appropriate output format."""
    match output_format:
        case OutputFormat.JSONL:
            emit_event(
                "state_change",
                {
                    "field": change.field,
                    "old_value": change.old_value,
                    "new_value": change.new_value,
                    "elapsed_seconds": change.elapsed_seconds,
                },
                OutputFormat.JSONL,
            )
        case OutputFormat.HUMAN:
            write_human_line(
                "{} changed: {} -> {} (after {:.1f}s)",
                change.field,
                change.old_value,
                change.new_value,
                change.elapsed_seconds,
            )
        case OutputFormat.JSON:
            # JSON mode: silent until final output
            pass
        case _ as unreachable:
            assert_never(unreachable)


def _output_result(result: WaitResult, output_opts: OutputOptions) -> None:
    """Output the final wait result."""
    result_data = {
        "target": result.target.identifier,
        "target_type": result.target.target_type.value,
        "is_matched": result.is_matched,
        "is_timed_out": result.is_timed_out,
        "matched_state": result.matched_state,
        "elapsed_seconds": round(result.elapsed_seconds, 2),
        "final_host_state": result.final_state.host_state.value if result.final_state.host_state else None,
        "final_agent_state": result.final_state.agent_state.value if result.final_state.agent_state else None,
        "state_changes": [
            {
                "field": c.field,
                "old_value": c.old_value,
                "new_value": c.new_value,
                "elapsed_seconds": round(c.elapsed_seconds, 2),
            }
            for c in result.state_changes
        ],
    }

    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json(result_data)
        case OutputFormat.JSONL:
            emit_event("result", result_data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            if result.is_matched:
                write_human_line(
                    "Target '{}' reached state {} (after {:.1f}s)",
                    result.target.identifier,
                    result.matched_state,
                    result.elapsed_seconds,
                )
            elif result.is_timed_out:
                write_human_line(
                    "Timed out waiting for '{}' (after {:.1f}s)",
                    result.target.identifier,
                    result.elapsed_seconds,
                )
            else:
                write_human_line(
                    "Wait ended for '{}' without match (after {:.1f}s)",
                    result.target.identifier,
                    result.elapsed_seconds,
                )
        case _ as unreachable:
            assert_never(unreachable)


@click.command()
@click.argument("target", required=False, default=None)
@click.argument("states", nargs=-1)
@optgroup.group("Wait options")
@optgroup.option(
    "--state",
    multiple=True,
    help="State to wait for [repeatable]. Can also be passed as positional args after TARGET.",
)
@optgroup.option(
    "--timeout",
    default=None,
    help="Maximum time to wait (e.g. '30s', '5m', '1h'). Default: wait forever.",
)
@optgroup.option(
    "--interval",
    default="5s",
    show_default=True,
    help="Poll interval (e.g. '5s', '1m'). Default: 5s.",
)
@add_common_options
@click.pass_context
def wait(ctx: click.Context, **kwargs: object) -> None:
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="wait",
        command_class=WaitCliOptions,
    )

    # Resolve the target identifier
    target_identifier = opts.target
    if target_identifier is None:
        target_identifier = _read_target_from_stdin()

    # Resolve the target
    resolved = resolve_wait_target(target_identifier, mng_ctx)

    # Combine positional states and --state option values
    all_state_args = list(opts.states) + list(opts.state)
    if all_state_args:
        target_states = validate_state_strings(all_state_args, ALL_VALID_STATE_STRINGS)
    else:
        target_states = compute_default_target_states(resolved.target.target_type)

    # Parse timeout
    timeout_seconds: float | None = None
    if opts.timeout is not None:
        timeout_seconds = parse_duration_to_seconds(opts.timeout)

    # Parse interval
    interval_seconds = parse_duration_to_seconds(opts.interval)

    # Poll the initial state
    initial_state = poll_target_state(resolved)
    current_state_description = describe_combined_state(initial_state, resolved.target.target_type)

    # Check if already in a target state
    already_matched = check_state_match(
        combined_state=initial_state,
        target_type=resolved.target.target_type,
        target_states=target_states,
    )
    if already_matched is not None:
        result = WaitResult(
            target=resolved.target,
            is_matched=True,
            is_timed_out=False,
            final_state=initial_state,
            matched_state=already_matched,
            elapsed_seconds=0.0,
            state_changes=(),
        )
        emit_info(
            f"Target '{resolved.target.identifier}' is already in state {already_matched} ({current_state_description})",
            output_opts.output_format,
        )
        _output_result(result, output_opts)
        ctx.exit(EXIT_CODE_SUCCESS)
        return

    # Log what we're waiting for
    sorted_states = ", ".join(sorted(target_states))
    emit_info(
        f"Waiting for {resolved.target.target_type.value.lower()} '{resolved.target.identifier}' "
        f"to transition from {current_state_description} to one of: {sorted_states}",
        output_opts.output_format,
    )
    if timeout_seconds is not None:
        logger.info("Timeout: {:.0f}s", timeout_seconds)
    logger.info("Poll interval: {:.0f}s", interval_seconds)

    # Run the wait loop
    captured_output_format = output_opts.output_format
    try:
        result = wait_for_state(
            target=resolved.target,
            poll_fn=lambda: poll_target_state(resolved),
            target_states=target_states,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            on_state_change=lambda change: _emit_state_change(change, captured_output_format),
        )
    except KeyboardInterrupt:
        logger.debug("Received keyboard interrupt")
        ctx.exit(EXIT_CODE_ERROR)
        return

    # Output the result
    _output_result(result, output_opts)

    # Set exit code
    if result.is_matched:
        ctx.exit(EXIT_CODE_SUCCESS)
    elif result.is_timed_out:
        ctx.exit(EXIT_CODE_TIMEOUT)
    else:
        ctx.exit(EXIT_CODE_ERROR)


CommandHelpMetadata(
    key="wait",
    one_line_description="Wait for an agent or host to reach a target state",
    synopsis="mng wait [TARGET] [STATE ...] [--state STATE ...] [--timeout DURATION] [--interval DURATION]",
    description="""Wait for an agent or host to transition to one of the specified states.

TARGET can be an agent ID (agent-*), host ID (host-*), or an agent/host name.
If TARGET is omitted, it is read from stdin (one line, must be an ID like agent-* or host-*).

States can be provided as positional arguments after TARGET, via the repeatable --state option, or both.
Valid states include all agent lifecycle states (STOPPED, RUNNING, WAITING, REPLACED, DONE) and
all host states (BUILDING, STARTING, RUNNING, STOPPING, STOPPED, PAUSED, CRASHED, FAILED, DESTROYED, UNAUTHENTICATED).

If no states are specified, waits for any terminal state (the target stops running).

When watching an agent, both agent and host states are tracked:
- STOPPED counts if either the agent or host is stopped
- RUNNING only counts if the agent itself is running (not the host)
- Host-specific states (CRASHED, PAUSED, etc.) are matched against the host

Exit codes:
  0 - Target reached one of the requested states
  1 - Error
  2 - Timeout expired""",
    examples=(
        ("Wait for an agent to finish", "mng wait my-agent DONE"),
        ("Wait for any terminal state", "mng wait agent-abc123"),
        ("Wait for agent to enter WAITING", "mng wait my-agent WAITING"),
        ("Wait with timeout", "mng wait my-agent DONE --timeout 5m"),
        ("Wait for host to stop", "mng wait host-xyz789 STOPPED"),
        ("Read target from stdin", "echo agent-abc123 | mng wait"),
        ("Multiple states", "mng wait my-agent --state WAITING --state DONE"),
    ),
    see_also=(("list", "List agents and their current states"),),
).register()

add_pager_help_option(wait)
