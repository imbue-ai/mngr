import json
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import assert_never

from imbue.mngr.api.preservation import find_preserved_agent_by_id
from imbue.mngr.api.preservation import read_preserved_common_transcript
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner

_HUMAN_OUTPUT_TRUNCATION_LENGTH = 500


def get_event_role(event: Mapping[str, Any]) -> str | None:
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


def parse_transcript_events(
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
        if roles and get_event_role(event) not in roles:
            continue
        events.append(event)
    return events


def format_event_human(event: Mapping[str, Any]) -> str:
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
            # Every emitter fills the ordered parts[]; render it directly (the flat
            # text + tool_calls are kept on the record as a convenience baseline, but
            # parts[] is the authoritative ordered view).
            lines: list[str] = []
            for part in event.get("parts", []):
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    content = part.get("content", "")
                    if content:
                        lines.append(content)
                elif part.get("type") == "tool_call":
                    tool_name = part.get("tool_name", "unknown")
                    preview = part.get("input_preview", "")
                    lines.append(f"  -> {tool_name}({preview})")
                else:
                    # Unknown part type (e.g. a future reasoning part): nothing to render here.
                    continue
            body = "\n".join(lines) if lines else "(no content)"
            return f"[{timestamp}] assistant:\n{body}"

        case "tool_result":
            tool_name = event.get("tool_name", "unknown")
            output = event.get("output", "")
            is_error = event.get("is_error", False)
            error_marker = " [ERROR]" if is_error else ""
            # Truncate long output for display
            if len(output) > _HUMAN_OUTPUT_TRUNCATION_LENGTH:
                output = output[:_HUMAN_OUTPUT_TRUNCATION_LENGTH] + "..."
            return f"[{timestamp}] tool ({tool_name}){error_marker}:\n{output}"

        case _:
            return f"[{timestamp}] {event_type}: {json.dumps(event)}"


def render_transcript_to_string(
    events: Sequence[Mapping[str, Any]],
    output_format: OutputFormat,
) -> str:
    """Render transcript events to a single string in the requested format."""
    match output_format:
        case OutputFormat.JSONL:
            return "".join(json.dumps(dict(event), separators=(",", ":")) + "\n" for event in events)

        case OutputFormat.JSON:
            return json.dumps([dict(event) for event in events], indent=2) + "\n"

        case OutputFormat.HUMAN:
            rendered_parts: list[str] = []
            for idx, event in enumerate(events):
                if idx > 0:
                    rendered_parts.append("\n")
                rendered_parts.append(format_event_human(event) + "\n")
            return "".join(rendered_parts)

        case _ as unreachable:
            assert_never(unreachable)


def apply_head_or_tail(
    events: Sequence[Mapping[str, Any]],
    head: int | None,
    tail: int | None,
) -> list[dict[str, Any]]:
    """Return the first ``head`` or last ``tail`` events (callers pass at most one)."""
    event_list = [dict(event) for event in events]
    if head is not None:
        return event_list[:head]
    if tail is not None:
        return event_list[-tail:]
    return event_list


def render_preserved_agent_transcript(
    host_dir: Path,
    agent_id: AgentId,
    roles: tuple[str, ...],
    head: int | None,
    tail: int | None,
    output_format: OutputFormat,
) -> str | None:
    """Render a destroyed agent's preserved common transcript, or None when none was preserved.

    Returns None when the agent has no preserved directory under ``host_dir`` or
    its preserved directory holds no common transcript file, so the caller can
    fall back to a live lookup or surface a 404.
    """
    info = find_preserved_agent_by_id(host_dir, agent_id)
    if info is None:
        return None
    content = read_preserved_common_transcript(host_dir, info)
    if content is None:
        return None
    events = parse_transcript_events(
        content,
        roles=roles,
        source_description=f"preserved transcript for agent '{info.agent_name}'",
    )
    return render_transcript_to_string(apply_head_or_tail(events, head=head, tail=tail), output_format)
