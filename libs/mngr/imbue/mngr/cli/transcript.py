import json
import os
import sys
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup

from imbue.mngr.api.events import EventsTarget
from imbue.mngr.api.events import discover_event_sources
from imbue.mngr.api.events import read_event_content
from imbue.mngr.api.events import resolve_events_target
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner


class TranscriptCliOptions(CommonCliOptions):
    """Options passed from the CLI to the transcript command."""

    target: str | None
    role: tuple[str, ...]
    tail: int | None
    head: int | None
    turn: int | None
    last_completed_turn: bool
    count_turns: bool
    list_turns: bool


_COMMON_TRANSCRIPT_SUFFIX = "common_transcript"
_USER_MESSAGE_TYPE = "user_message"
_LIST_TURNS_PREVIEW_CHARS = 80
_AGENT_ID_ENV_VAR = "MNGR_AGENT_ID"


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
        raise MngrError(
            f"No common transcript found for {target.display_name}. "
            "The agent may not have produced any transcript events yet."
        )
    if len(matching_sources) > 1:
        source_paths = ", ".join(s.source_path for s in matching_sources)
        raise MngrError(
            f"Multiple common transcript sources found for {target.display_name}: {source_paths}. "
            "This is unexpected -- please report this as a bug."
        )
    return matching_sources[0].source_path


def _parse_transcript_events(
    content: str,
    roles: tuple[str, ...],
    source_description: str,
) -> list[dict[str, Any]]:
    """Parse JSONL content into transcript events, optionally filtering by role."""
    events: list[dict[str, Any]] = []
    warner = MalformedJsonLineWarner(source_description=source_description)
    for line in content.splitlines():
        parsed = warner.parse(line)
        if parsed is None:
            continue
        event, _ = parsed
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


def _user_message_indices(events: list[dict[str, Any]]) -> list[int]:
    """Return the indices (into events) of every user_message event.

    Stop-hook injections and other framework-meta events are already
    reclassified to ``tool_result`` (with ``tool_name == "meta"``) by the
    common_transcript producer, so a plain ``type == "user_message"`` check
    is sufficient.
    """
    return [i for i, event in enumerate(events) if event.get("type") == _USER_MESSAGE_TYPE]


def _resolve_turn_index(turn: int, turn_count: int) -> int:
    """Normalize a 1-indexed or negative turn argument to a 0-based index into the user_message list.

    Raises UserInputError if ``turn`` is 0, out of range, or the transcript has no turns.
    """
    if turn_count == 0:
        raise UserInputError("Transcript has no turns (no user_message events).")
    if turn == 0:
        raise UserInputError("--turn is 1-indexed; use --turn 1 for the first turn or --turn -1 for the last.")
    if turn > 0:
        if turn > turn_count:
            raise UserInputError(f"--turn {turn} out of range; transcript has {turn_count} turn(s).")
        return turn - 1
    if -turn > turn_count:
        raise UserInputError(f"--turn {turn} out of range; transcript has {turn_count} turn(s).")
    return turn_count + turn


def _slice_for_turn(
    events: list[dict[str, Any]], turn_starts: list[int], zero_based_turn: int
) -> list[dict[str, Any]]:
    """Return events from turn_starts[zero_based_turn] (inclusive) to turn_starts[zero_based_turn + 1] (exclusive).

    If the turn is the last one, the slice runs to end-of-transcript.
    """
    start = turn_starts[zero_based_turn]
    if zero_based_turn + 1 < len(turn_starts):
        end = turn_starts[zero_based_turn + 1]
        return events[start:end]
    return events[start:]


def _build_turn_summary(events: list[dict[str, Any]], turn_starts: list[int]) -> list[dict[str, Any]]:
    """Build summary records for --list-turns, one per turn boundary."""
    summaries: list[dict[str, Any]] = []
    for ordinal, event_index in enumerate(turn_starts, start=1):
        event = events[event_index]
        content = str(event.get("content", ""))
        preview = " ".join(content.split())
        if len(preview) > _LIST_TURNS_PREVIEW_CHARS:
            preview = preview[:_LIST_TURNS_PREVIEW_CHARS] + "..."
        summaries.append(
            {
                "turn": ordinal,
                "timestamp": event.get("timestamp", ""),
                "event_id": event.get("event_id", ""),
                "content_preview": preview,
            }
        )
    return summaries


def _emit_turn_summary(summaries: list[dict[str, Any]], output_opts: OutputOptions) -> None:
    """Emit turn-summary records in the requested format."""
    match output_opts.output_format:
        case OutputFormat.JSONL:
            for s in summaries:
                write_human_line(json.dumps(s, separators=(",", ":")))

        case OutputFormat.JSON:
            write_human_line(json.dumps(summaries, indent=2))

        case OutputFormat.HUMAN:
            if not summaries:
                write_human_line("(no turns)")
                return
            header = f"{'#':>4}  {'timestamp':<24}  preview"
            write_human_line(header)
            write_human_line("-" * len(header))
            for s in summaries:
                write_human_line(f"{s['turn']:>4}  {str(s['timestamp']):<24}  {s['content_preview']}")

        case _ as unreachable:
            assert_never(unreachable)


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


def _resolve_target_identifier(target: str | None) -> str:
    """Return the explicit target if given, else fall back to MNGR_AGENT_ID.

    Raises UserInputError if neither is available.
    """
    if target is not None and target != "":
        return target
    env_target = os.environ.get(_AGENT_ID_ENV_VAR)
    if env_target:
        return env_target
    raise UserInputError(
        f"No target given and `{_AGENT_ID_ENV_VAR}` is not set. Pass an agent name/ID, or run inside an agent context."
    )


def _validate_turn_options(opts: TranscriptCliOptions) -> None:
    """Reject incompatible combinations of head/tail and the turn-family flags."""
    if opts.head is not None and opts.tail is not None:
        raise UserInputError("Cannot specify both --head and --tail")

    turn_flags = [
        ("--turn", opts.turn is not None),
        ("--last-completed-turn", opts.last_completed_turn),
        ("--count-turns", opts.count_turns),
        ("--list-turns", opts.list_turns),
    ]
    active_turn_flags = [name for name, is_active in turn_flags if is_active]
    if len(active_turn_flags) > 1:
        raise UserInputError(
            f"Cannot specify more than one of {', '.join(active_turn_flags)}; these flags are mutually exclusive."
        )
    if active_turn_flags and (opts.head is not None or opts.tail is not None):
        slicing = "--head" if opts.head is not None else "--tail"
        raise UserInputError(f"Cannot combine {active_turn_flags[0]} with {slicing}.")


@click.command(name="transcript")
@click.argument("target", default=None, required=False)
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
@optgroup.group("Turns")
@optgroup.option(
    "--turn",
    type=int,
    default=None,
    help=(
        "Extract a single turn by 1-indexed position. Negative indices count from the end "
        "(--turn -1 is the last/in-progress turn, --turn -2 is the previous completed turn). "
        "A 'turn' is the slice from one user_message (inclusive) up to the next user_message (exclusive)."
    ),
)
@optgroup.option(
    "--last-completed-turn",
    is_flag=True,
    default=False,
    help="Extract the most recent completed turn (equivalent to --turn -2).",
)
@optgroup.option(
    "--count-turns",
    is_flag=True,
    default=False,
    help="Print the number of turns in the transcript and exit.",
)
@optgroup.option(
    "--list-turns",
    is_flag=True,
    default=False,
    help="List each turn's number, timestamp, and a content preview instead of the events themselves.",
)
@add_common_options
@click.pass_context
def transcript(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="transcript",
        command_class=TranscriptCliOptions,
        is_format_template_supported=False,
    )

    _validate_turn_options(opts)
    target_identifier = _resolve_target_identifier(opts.target)

    # Resolve the target agent
    target = resolve_events_target(
        identifier=target_identifier,
        mngr_ctx=mngr_ctx,
    )

    # Find the common_transcript source
    source_path = _find_common_transcript_source(target)
    event_file_name = f"{source_path}/events.jsonl"

    # Read the transcript file
    try:
        content = read_event_content(target, event_file_name)
    except (MngrError, OSError) as e:
        raise MngrError(f"Failed to read transcript for {target.display_name}: {e}") from e

    # For turn-aware operations we need unfiltered events to compute boundaries,
    # then apply role filtering to the resulting slice. For other operations we
    # can filter at parse time.
    turn_mode = opts.turn is not None or opts.last_completed_turn or opts.count_turns or opts.list_turns
    parse_roles: tuple[str, ...] = () if turn_mode else opts.role
    all_events = _parse_transcript_events(
        content,
        roles=parse_roles,
        source_description=f"transcript file '{event_file_name}' for {target.display_name}",
    )

    if opts.count_turns:
        count = len(_user_message_indices(all_events))
        write_human_line(str(count))
        return

    if opts.list_turns:
        summaries = _build_turn_summary(all_events, _user_message_indices(all_events))
        _emit_turn_summary(summaries, output_opts)
        return

    if opts.turn is not None or opts.last_completed_turn:
        turn_starts = _user_message_indices(all_events)
        if opts.last_completed_turn:
            if len(turn_starts) < 2:
                raise UserInputError(f"No completed turn yet (only {len(turn_starts)} user message(s) in transcript).")
            zero_based = len(turn_starts) - 2
        else:
            assert opts.turn is not None
            zero_based = _resolve_turn_index(opts.turn, len(turn_starts))
        sliced = _slice_for_turn(all_events, turn_starts, zero_based)
        if opts.role:
            sliced = [e for e in sliced if _get_event_role(e) in opts.role]
        _emit_transcript(sliced, output_opts)
        return

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
    synopsis=(
        "mngr transcript [TARGET] [--role ROLE] [--tail N | --head N | --turn N | --last-completed-turn"
        " | --count-turns | --list-turns] [--format human|json|jsonl]"
    ),
    arguments_description=(
        "- `TARGET`: Agent name or ID whose transcript to view. Optional when "
        "the command runs inside an agent context that exports `MNGR_AGENT_ID`."
    ),
    description="""View the common transcript for an agent. The transcript contains
user messages, assistant messages, and tool call/result summaries in a
common, agent-agnostic format.

The command automatically finds the correct transcript file regardless
of the agent type (e.g. claude, codex). If TARGET is omitted, the
command resolves the current agent from the `MNGR_AGENT_ID` environment
variable that mngr exports into every agent's shell.

Use --role to filter by message role (user, assistant, tool). This
option is repeatable to include multiple roles.

Turn-aware options operate on conversational turns, where each
`user_message` event marks a turn boundary. They are mutually exclusive
with each other and with --head / --tail:
  - --turn N: extract a single turn. Positive N is 1-indexed from the
    start; negative N counts from the end (--turn -1 = last/in-progress,
    --turn -2 = previous completed).
  - --last-completed-turn: shortcut for --turn -2.
  - --count-turns: print just the turn count and exit.
  - --list-turns: summary table of turn boundaries (respects --format).

Use --format to control output:
  - human (default): nicely formatted, readable output
  - jsonl: raw JSONL, one event per line (for piping)
  - json: full JSON array (for programmatic use)""",
    examples=(
        ("View full transcript", "mngr transcript my-agent"),
        ("View only user messages", "mngr transcript my-agent --role user"),
        ("View user and assistant messages", "mngr transcript my-agent --role user --role assistant"),
        ("View last 20 events", "mngr transcript my-agent --tail 20"),
        ("Output as JSONL for piping", "mngr transcript my-agent --format jsonl"),
        ("Output as JSON", "mngr transcript my-agent --format json"),
        ("Count turns in the transcript", "mngr transcript my-agent --count-turns"),
        (
            "Extract the previous completed turn (from inside an agent)",
            "mngr transcript --last-completed-turn --format jsonl",
        ),
        ("Extract the second turn", "mngr transcript my-agent --turn 2 --format jsonl"),
        ("List all turn boundaries", "mngr transcript my-agent --list-turns"),
    ),
    see_also=(
        ("event", "View all events from an agent or host"),
        ("message", "Send a message to an agent"),
    ),
).register()

# Add pager-enabled help option to the transcript command
add_pager_help_option(transcript)
