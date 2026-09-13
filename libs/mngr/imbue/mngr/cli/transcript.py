import json
import sys
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mngr.agents.common_transcript_records import LEGACY_RECORD_TYPE_NAMES
from imbue.mngr.agents.common_transcript_records import OLD_FORMAT_STREAM_EXPLANATION
from imbue.mngr.api.events import read_common_transcript_content
from imbue.mngr.api.events import resolve_events_target
from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.trajectory import build_trajectory_for_agent
from imbue.mngr.cli.address_params import AGENT_OR_HOST_ADDRESS
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentOrHostAddress
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner


class TranscriptCliOptions(CommonCliOptions):
    """Options passed from the CLI to the transcript command."""

    target: AgentOrHostAddress
    role: tuple[str, ...]
    tail: int | None
    head: int | None
    output: str | None
    full: bool


# The transcript-specific --format value that emits a full ATIF trajectory document
# instead of rendering the stream (see specs/atif-transcript-alignment/spec.md).
_ATIF_FORMAT_NAME = "atif"

# Display-time truncation caps for human rendering; --full disables them.
_TOOL_INPUT_DISPLAY_LIMIT = 200
_TOOL_OUTPUT_DISPLAY_LIMIT = 2000
_REASONING_DISPLAY_LIMIT = 500

# The roles --role accepts: the ATIF step sources plus 'tool' for observation records.
_ROLE_VOCABULARY: Final[tuple[str, ...]] = ("user", "agent", "system", "tool")


class OldFormatTranscriptError(MngrError):
    """Raised when an agent's stream predates the ATIF cutover and so cannot be rendered."""

    user_help_text = (
        "The raw native transcript is still available under logs/<agent_type>_transcript/, and "
        "'mngr event <agent>' still shows the raw records."
    )


def _truncate_for_display(text: str, limit: int, is_full: bool) -> str:
    if is_full or len(text) <= limit:
        return text
    return text[:limit] + "..."


def _dict_items(value: Any) -> list[dict[str, Any]]:
    """The dict entries of a value that should be a list of objects (empty if it is not)."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _observation_results(observation: Any) -> list[dict[str, Any]]:
    """The result objects of a value that should be an ATIF observation (empty if it is not)."""
    return _dict_items(observation.get("results")) if isinstance(observation, dict) else []


def _render_content(content: Any) -> str:
    """Render an ATIF content value: a plain string, or a list of multimodal content parts.

    Image parts have no text to show, so they render as a placeholder naming their
    media type; text parts contribute their text.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    rendered: list[str] = []
    for part in _dict_items(content):
        match part.get("type"):
            case "text":
                rendered.append(str(part.get("text") or ""))
            case "image":
                source = part.get("source")
                media_type = source.get("media_type", "unknown") if isinstance(source, dict) else "unknown"
                rendered.append(f"[image: {media_type}]")
            case _:
                continue
    return "\n".join(rendered)


def _assert_agent_type_supports_transcripts(address: AgentOrHostAddress, mngr_ctx: MngrContext) -> None:
    """Raise UserInputError if the targeted agent's type does not implement HasCommonTranscriptMixin.

    No-op for HostAddress targets; only agent-scoped lookups are validated here.
    """
    if not isinstance(address, AgentAddress):
        return
    _host_ref, agent_ref = find_one_agent(address, mngr_ctx)
    agent_type = agent_ref.agent_type
    if agent_type is None:
        # Agent's data.json lacks 'type'; defer to downstream error rather than blocking.
        return
    # Resolve through the parent_type chain (and aliases) so a config-defined
    # subtype (a custom [agent_types.X] whose parent_type is e.g. 'claude') maps
    # to its parent's class, rather than failing a flat class-registry lookup.
    # A type we cannot resolve raises here (UnknownAgentTypeError, or MngrError if
    # its plugin is disabled), which is correct: if we cannot resolve the type we
    # do not know how to read it, so block rather than fall through to discovery.
    resolved = resolve_agent_type(agent_type, mngr_ctx.config)
    if issubclass(resolved.agent_class, HasCommonTranscriptMixin):
        return
    raise UserInputError(
        f"Agent '{agent_ref.agent_name}' has type '{agent_type}', which does not produce a common transcript."
    )


def _parse_transcript_events(
    content: str,
    roles: tuple[str, ...],
    source_description: str,
) -> list[dict[str, Any]]:
    """Parse JSONL content into transcript events, optionally filtering by role.

    Raises :class:`OldFormatTranscriptError` for a stream carrying the retired
    pre-ATIF record types: its records cannot be rendered, and the check runs
    before role filtering so it fires whatever filters were passed.
    """
    events: list[dict[str, Any]] = []
    record_count = 0
    warner = MalformedJsonLineWarner(source_description=source_description)
    for line in content.splitlines():
        parsed = warner.parse(line)
        if parsed is None:
            continue
        event, _ = parsed
        record_count += 1
        record_type = event.get("type")
        if record_type in LEGACY_RECORD_TYPE_NAMES:
            raise OldFormatTranscriptError(
                f"The {source_description} uses the retired pre-ATIF format ('{record_type}' records) and "
                f"cannot be rendered: {OLD_FORMAT_STREAM_EXPLANATION}"
            )
        if roles and _get_event_role(event) not in roles:
            continue
        events.append(event)
    # A filter that matches nothing on a stream that does have records is almost always a
    # mistyped role, which would otherwise look like an empty transcript.
    if roles and record_count > 0 and len(events) == 0:
        logger.warning(
            "No records in the {} match --role {} (valid roles: {})",
            source_description,
            ", ".join(roles),
            ", ".join(_ROLE_VOCABULARY),
        )
    return events


def _get_event_role(event: dict[str, Any]) -> str | None:
    """Extract the role from a common transcript event.

    A step's role is its ATIF source ('user' | 'agent' | 'system'), an
    observation's role is 'tool', and the header (stream framing) has none.
    """
    match event.get("type", ""):
        case "step":
            source = event.get("source")
            return str(source) if source is not None else None
        case "observation":
            return "tool"
        case _:
            return None


def _format_atif_step_human(event: dict[str, Any], timestamp: str, is_full: bool) -> str:
    source = event.get("source", "unknown")
    lines: list[str] = []
    reasoning = event.get("reasoning_content")
    if reasoning:
        lines.append(f"  (thinking) {_truncate_for_display(str(reasoning), _REASONING_DISPLAY_LIMIT, is_full)}")
    message = event.get("message", "")
    if message:
        lines.append(str(message))
    for tool_call in _dict_items(event.get("tool_calls")):
        function_name = tool_call.get("function_name", "unknown")
        arguments_preview = _truncate_for_display(
            json.dumps(tool_call.get("arguments") or {}, separators=(",", ":")), _TOOL_INPUT_DISPLAY_LIMIT, is_full
        )
        lines.append(f"  -> {function_name}({arguments_preview})")
    # System steps may carry their result inline.
    for result in _observation_results(event.get("observation")):
        content = _render_content(result.get("content") or "")
        if content:
            lines.append(_truncate_for_display(content, _TOOL_OUTPUT_DISPLAY_LIMIT, is_full))
    body = "\n".join(lines) if lines else "(no content)"
    return f"[{timestamp}] {source}:\n{body}"


def _format_atif_observation_human(event: dict[str, Any], timestamp: str, is_full: bool) -> str:
    blocks: list[str] = []
    for result in _observation_results(event):
        extra = result.get("extra")
        extra_fields = extra if isinstance(extra, dict) else {}
        tool_name = extra_fields.get("tool_name", "unknown")
        error_marker = " [ERROR]" if extra_fields.get("is_error") else ""
        content = _truncate_for_display(
            _render_content(result.get("content") or ""), _TOOL_OUTPUT_DISPLAY_LIMIT, is_full
        )
        blocks.append(f"[{timestamp}] tool ({tool_name}){error_marker}:\n{content}")
    if not blocks:
        return f"[{timestamp}] tool: (no results)"
    return "\n\n".join(blocks)


def _format_event_human(event: dict[str, Any], is_full: bool) -> str:
    """Format a single transcript event for human-readable display."""
    event_type = event.get("type", "unknown")
    timestamp = event.get("timestamp", "")

    # Trim sub-second precision for readability
    if "." in timestamp:
        timestamp = timestamp.split(".")[0] + "Z"

    match event_type:
        case "step":
            return _format_atif_step_human(event, timestamp, is_full)

        case "observation":
            return _format_atif_observation_human(event, timestamp, is_full)

        case _:
            return f"[{timestamp}] {event_type}: {json.dumps(event)}"


def _emit_transcript(
    events: list[dict[str, Any]],
    output_opts: OutputOptions,
    is_full: bool,
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
                sys.stdout.write(_format_event_human(event, is_full) + "\n")
            sys.stdout.flush()

        case _ as unreachable:
            assert_never(unreachable)


def _emit_atif_document(target: AgentAddress, output_path: str | None, mngr_ctx: MngrContext) -> None:
    """Build the agent's stream into a full ATIF document and write it out."""
    build_result = build_trajectory_for_agent(target, mngr_ctx)
    for warning in build_result.warnings:
        logger.warning("Trajectory build: {}", warning)
    document_json = json.dumps(build_result.trajectory.to_json_dict(), indent=2) + "\n"
    if output_path is None:
        sys.stdout.write(document_json)
        sys.stdout.flush()
        return
    try:
        Path(output_path).write_text(document_json)
    except OSError as e:
        raise UserInputError(f"Could not write the ATIF document to '{output_path}': {e}") from e
    logger.info("Wrote ATIF trajectory to {}", output_path)


@click.command(name="transcript")
@click.argument("target", type=AGENT_OR_HOST_ADDRESS)
@optgroup.group("Filtering")
@optgroup.option(
    "--role",
    multiple=True,
    help="Only show messages with this role (repeatable; user, agent, system, tool)",
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
@optgroup.option(
    "--output",
    type=click.Path(),
    default=None,
    help="Write the built ATIF document to this file instead of stdout (only with --format atif)",
)
@optgroup.option(
    "--full",
    is_flag=True,
    default=False,
    help="Disable display-time truncation of tool inputs/outputs and thinking (human output only)",
)
@add_common_options
@click.pass_context
def transcript(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="transcript",
        command_class=TranscriptCliOptions,
        is_format_template_supported=False,
        extra_builtin_format_names=frozenset({_ATIF_FORMAT_NAME}),
    )

    if opts.head is not None and opts.tail is not None:
        raise UserInputError("Cannot specify both --head and --tail")

    # --format atif emits a whole document rather than a rendered event list, so the
    # rendering flags do not apply to it and --output applies to nothing else.
    atif_target: AgentAddress | None = None
    if output_opts.extra_format == _ATIF_FORMAT_NAME:
        if opts.role or opts.head is not None or opts.tail is not None:
            raise UserInputError(
                "--role/--head/--tail do not apply to --format atif (the document is always complete)"
            )
        if not isinstance(opts.target, AgentAddress):
            raise UserInputError("--format atif requires an agent target (not a host)")
        atif_target = opts.target
    if atif_target is None and opts.output is not None:
        raise UserInputError("--output is only supported with --format atif")

    # Fail fast with a clear error when the agent type does not produce a common transcript.
    _assert_agent_type_supports_transcripts(opts.target, mngr_ctx)

    if atif_target is not None:
        _emit_atif_document(atif_target, opts.output, mngr_ctx)
        return

    # Resolve the target agent
    target = resolve_events_target(
        address=opts.target,
        mngr_ctx=mngr_ctx,
    )

    # Read the transcript file
    event_file_name, content = read_common_transcript_content(target)

    # Parse and filter events
    all_events = _parse_transcript_events(
        content,
        roles=opts.role,
        source_description=f"transcript file '{event_file_name}' for {target.display_name}",
    )

    # The stream header is container framing, not conversation content, so human
    # output drops it before windowing -- otherwise `--head 1` spends its window on
    # a record it will not render. JSON/JSONL emit the raw stream, header included.
    if output_opts.output_format == OutputFormat.HUMAN:
        all_events = [event for event in all_events if event.get("type") != "header"]

    # Apply head/tail
    if opts.head is not None:
        all_events = all_events[: opts.head]
    elif opts.tail is not None:
        all_events = all_events[-opts.tail :]
    else:
        pass

    # Emit
    _emit_transcript(all_events, output_opts, is_full=opts.full)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="transcript",
    one_line_description="View the message transcript for an agent",
    synopsis="mngr transcript TARGET [--role ROLE] [--tail N] [--head N] [--full] [--format human|json|jsonl|atif] [--output PATH]",
    arguments_description="- `TARGET`: Agent name or ID whose transcript to view",
    description="""View the common transcript for an agent. The transcript contains
user turns, agent turns, and tool results in a common, agent-agnostic format.

The command automatically finds the correct transcript file regardless
of the agent type (e.g. claude, codex).

Use --role to filter by message role (user, agent, system, tool). This
option is repeatable to include multiple roles.

Human output truncates long tool inputs, tool outputs, and thinking for
readability; pass --full to see them untruncated. Only the display is
truncated -- the underlying stream (and --format json/jsonl/atif) always
carries the complete text.

Use --format to control output:
  - human (default): nicely formatted, readable output
  - jsonl: raw JSONL, one event per line (for piping)
  - json: full JSON array (for programmatic use)
  - atif: a single validated ATIF trajectory document assembled from the
    stream (Agent Trajectory Interchange Format; embeds resolvable
    subagent trajectories). Use --output PATH to write it to a file.""",
    examples=(
        ("View full transcript", "mngr transcript my-agent"),
        ("View only user messages", "mngr transcript my-agent --role user"),
        ("View user and agent messages", "mngr transcript my-agent --role user --role agent"),
        ("View last 20 events", "mngr transcript my-agent --tail 20"),
        ("Output as JSONL for piping", "mngr transcript my-agent --format jsonl"),
        ("Output as JSON", "mngr transcript my-agent --format json"),
        ("Build a full ATIF trajectory document", "mngr transcript my-agent --format atif"),
    ),
    see_also=(
        ("event", "View all events from an agent or host"),
        ("message", "Send a message to an agent"),
    ),
).register()

# Add pager-enabled help option to the transcript command
add_pager_help_option(transcript)
