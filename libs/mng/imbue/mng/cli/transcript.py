import json
import sys
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mng.api.events import EventsTarget
from imbue.mng.api.events import discover_event_sources
from imbue.mng.api.events import read_event_content
from imbue.mng.api.events import resolve_events_target
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.config.data_types import CommonCliOptions
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.errors import MngError
from imbue.mng.errors import UserInputError
from imbue.mng.primitives import OutputFormat


class TranscriptCliOptions(CommonCliOptions):
    """Options passed from the CLI to the transcript command."""

    target: str
    role: tuple[str, ...]
    tail: int | None
    head: int | None


_COMMON_TRANSCRIPT_SUFFIX = "common_transcript"


def _find_common_transcript_source(target: EventsTarget) -> str:
    """Find the event source path ending with 'common_transcript'.

    Discovers all event sources for the target and returns the one whose
    source_path ends with 'common_transcript' (e.g. 'claude/common_transcript').
    This allows the user to not need to know the agent type prefix.
    """
    sources = discover_event_sources(target)
    matching_sources = [
        s
        for s in sources
        if (
            (s.source_path == _COMMON_TRANSCRIPT_SUFFIX or s.source_path.endswith(f"/{_COMMON_TRANSCRIPT_SUFFIX}"))
            and not s.source_path.startswith("logs/")
        )
    ]
    if len(matching_sources) == 0:
        raise MngError(
            f"No common transcript found for {target.display_name}. "
            "The agent may not have produced any transcript events yet."
        )
    if len(matching_sources) > 1:
        source_paths = ", ".join(s.source_path for s in matching_sources)
        raise MngError(
            f"Multiple common transcript sources found for {target.display_name}: {source_paths}. "
            "This is unexpected -- please report this as a bug."
        )
    return matching_sources[0].source_path


def _parse_transcript_events(
    content: str,
    roles: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Parse JSONL content into transcript events, optionally filtering by role."""
    events: list[dict[str, Any]] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError as e:
            logger.trace("Skipped malformed JSON line in transcript: {}", e)
            continue
        if roles and _get_event_role(event) not in roles:
            continue
        events.append(event)
    return events


def _get_event_role(event: dict[str, Any]) -> str | None:
    """Extract the role from a common transcript event.

    The role is either an explicit 'role' field, or derived from the event type:
    - user_message -> 'user'
    - assistant_message -> 'assistant'
    - tool_result -> 'tool'
    """
    role = event.get("role")
    if role is not None:
        return str(role)
    match event.get("type", ""):
        case "user_message":
            return "user"
        case "assistant_message":
            return "assistant"
        case "tool_result":
            return "tool"
        case _:
            return None


def _format_event_human(event: dict[str, Any]) -> str:
    """Format a single transcript event for human-readable display."""
    event_type = event.get("type", "unknown")
    timestamp = event.get("timestamp", "")

    # Trim sub-second precision for readability
    if "." in timestamp:
        timestamp = timestamp.split(".")[0] + "Z"

    match event_type:
        case "user_message":
            content = event.get("content", "")
            return f"[{timestamp}] user:\n{content}"

        case "assistant_message":
            text = event.get("text", "")
            tool_calls = event.get("tool_calls", [])
            parts: list[str] = []
            if text:
                parts.append(text)
            for tc in tool_calls:
                tool_name = tc.get("tool_name", "unknown")
                preview = tc.get("input_preview", "")
                parts.append(f"  -> {tool_name}({preview})")
            body = "\n".join(parts) if parts else "(no content)"
            return f"[{timestamp}] assistant:\n{body}"

        case "tool_result":
            tool_name = event.get("tool_name", "unknown")
            output = event.get("output", "")
            is_error = event.get("is_error", False)
            error_marker = " [ERROR]" if is_error else ""
            # Truncate long output for display
            if len(output) > 500:
                output = output[:500] + "..."
            return f"[{timestamp}] tool ({tool_name}){error_marker}:\n{output}"

        case _:
            return f"[{timestamp}] {event_type}: {json.dumps(event)}"


def _emit_transcript(
    events: list[dict[str, Any]],
    output_opts: OutputOptions,
) -> None:
    """Emit transcript events in the requested format."""
    match output_opts.output_format:
        case OutputFormat.JSONL:
            for event in events:
                sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
            sys.stdout.flush()

        case OutputFormat.JSON:
            sys.stdout.write(json.dumps(events, indent=2) + "\n")
            sys.stdout.flush()

        case OutputFormat.HUMAN:
            for idx, event in enumerate(events):
                if idx > 0:
                    sys.stdout.write("\n")
                sys.stdout.write(_format_event_human(event) + "\n")
            sys.stdout.flush()

        case _ as unreachable:
            assert_never(unreachable)


@click.command(name="transcript")
@click.argument("target")
@optgroup.group("Filtering")
@optgroup.option(
    "--role",
    multiple=True,
    help="Only show messages with this role (repeatable; e.g. user, assistant, tool)",
)
@optgroup.group("Display")
@optgroup.option(
    "--tail",
    type=click.IntRange(min=1),
    default=None,
    help="Show only the last N transcript events",
)
@optgroup.option(
    "--head",
    type=click.IntRange(min=1),
    default=None,
    help="Show only the first N transcript events",
)
@add_common_options
@click.pass_context
def transcript(ctx: click.Context, **kwargs: Any) -> None:
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="transcript",
        command_class=TranscriptCliOptions,
        is_format_template_supported=False,
    )

    if opts.head is not None and opts.tail is not None:
        raise UserInputError("Cannot specify both --head and --tail")

    # Resolve the target agent
    target = resolve_events_target(
        identifier=opts.target,
        mng_ctx=mng_ctx,
    )

    # Find the common_transcript source
    source_path = _find_common_transcript_source(target)
    event_file_name = f"{source_path}/events.jsonl"

    # Read the transcript file
    try:
        content = read_event_content(target, event_file_name)
    except (MngError, OSError) as e:
        raise MngError(f"Failed to read transcript for {target.display_name}: {e}") from e

    # Parse and filter events
    all_events = _parse_transcript_events(content, roles=opts.role)

    # Apply head/tail
    if opts.head is not None:
        all_events = all_events[: opts.head]
    elif opts.tail is not None:
        all_events = all_events[-opts.tail :]
    else:
        pass

    # Emit
    _emit_transcript(all_events, output_opts)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="transcript",
    one_line_description="View the message transcript for an agent",
    synopsis="mng transcript TARGET [--role ROLE] [--tail N] [--head N] [--format human|json|jsonl]",
    arguments_description="- `TARGET`: Agent name or ID whose transcript to view",
    description="""View the common transcript for an agent. The transcript contains
user messages, assistant messages, and tool call/result summaries in a
common, agent-agnostic format.

The command automatically finds the correct transcript file regardless
of the agent type (e.g. claude, codex).

Use --role to filter by message role (user, assistant, tool). This
option is repeatable to include multiple roles.

Use --format to control output:
  - human (default): nicely formatted, readable output
  - jsonl: raw JSONL, one event per line (for piping)
  - json: full JSON array (for programmatic use)""",
    examples=(
        ("View full transcript", "mng transcript my-agent"),
        ("View only user messages", "mng transcript my-agent --role user"),
        ("View user and assistant messages", "mng transcript my-agent --role user --role assistant"),
        ("View last 20 events", "mng transcript my-agent --tail 20"),
        ("Output as JSONL for piping", "mng transcript my-agent --format jsonl"),
        ("Output as JSON", "mng transcript my-agent --format json"),
    ),
    see_also=(
        ("events", "View all events from an agent or host"),
        ("message", "Send a message to an agent"),
    ),
).register()

# Add pager-enabled help option to the transcript command
add_pager_help_option(transcript)
