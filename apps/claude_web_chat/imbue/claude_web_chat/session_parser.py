"""Parse raw Claude session JSONL files into common transcript events.

Reimplements the conversion logic from mngr_claude's common_transcript.sh
in pure Python. Handles user messages, assistant messages with tool calls,
and tool result events.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_MAX_INPUT_PREVIEW_LENGTH = 200
_MAX_OUTPUT_LENGTH = 2000

_SOURCE = "claude/common_transcript"


def _extract_text_content(content: str | list[dict[str, Any]] | Any) -> str:
    """Extract plain text from a message content field (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if text:
                parts.append(text)
    return "\n".join(parts)


def _has_tool_results_only(content: str | list[Any] | Any) -> bool:
    """Check if a content list contains only tool_result blocks (no user text)."""
    if isinstance(content, str):
        return False
    if not isinstance(content, list):
        return True
    for block in content:
        if isinstance(block, dict):
            block_type = block.get("type", "")
            if block_type not in ("tool_result",):
                return False
        elif isinstance(block, str):
            return False
    return True


def _make_event_id(uuid: str, suffix: str) -> str:
    """Derive a deterministic event_id from the source UUID and a suffix."""
    return f"{uuid}-{suffix}"


def parse_session_lines(
    lines: list[str],
    existing_event_ids: set[str] | None = None,
    tool_name_by_call_id: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Parse raw Claude session JSONL lines into common transcript events.

    Args:
        lines: Raw JSONL lines from a Claude session file.
        existing_event_ids: Set of event IDs already emitted, for deduplication.
            If None, no deduplication is performed.
        tool_name_by_call_id: Mutable mapping from tool_use_id to tool_name,
            carried across calls for cross-message tool name resolution.
            If None, a fresh dict is used.

    Returns:
        List of common transcript event dicts, sorted by timestamp.
    """
    if existing_event_ids is None:
        existing_event_ids = set()
    if tool_name_by_call_id is None:
        tool_name_by_call_id = {}

    new_events: list[tuple[str, dict[str, Any]]] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type: str = raw.get("type", "")
        uuid: str = raw.get("uuid", "")
        timestamp: str = raw.get("timestamp", "")

        if not uuid or not timestamp:
            continue

        if event_type == "assistant":
            _parse_assistant_message(raw, uuid, timestamp, existing_event_ids, tool_name_by_call_id, new_events)
        elif event_type == "user":
            _parse_user_message(raw, uuid, timestamp, existing_event_ids, tool_name_by_call_id, new_events)
        # Skip: progress, file-history-snapshot, system, result, etc.

    new_events.sort(key=lambda x: x[0])
    return [event for _, event in new_events]


def _parse_assistant_message(
    raw: dict[str, Any],
    uuid: str,
    timestamp: str,
    existing_event_ids: set[str],
    tool_name_by_call_id: dict[str, str],
    new_events: list[tuple[str, dict[str, Any]]],
) -> None:
    event_id = _make_event_id(uuid, "assistant")
    if event_id in existing_event_ids:
        return

    message: dict[str, Any] = raw.get("message", {})
    content_blocks: list[Any] = message.get("content", [])
    model: str = message.get("model", "unknown")
    stop_reason: str | None = message.get("stop_reason")
    usage_raw: dict[str, Any] = message.get("usage", {})

    text_parts: list[str] = []
    tool_calls: list[dict[str, str]] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")
        if block_type == "text":
            text = block.get("text", "")
            if text:
                text_parts.append(text)
        elif block_type == "tool_use":
            call_id: str = block.get("id", "")
            tool_name: str = block.get("name", "")
            tool_input = block.get("input", {})
            input_preview = json.dumps(tool_input, separators=(",", ":"))
            if len(input_preview) > _MAX_INPUT_PREVIEW_LENGTH:
                input_preview = input_preview[:_MAX_INPUT_PREVIEW_LENGTH] + "..."

            if call_id and tool_name:
                tool_name_by_call_id[call_id] = tool_name

            tool_calls.append({
                "tool_call_id": call_id,
                "tool_name": tool_name,
                "input_preview": input_preview,
            })

    usage: dict[str, Any] | None = None
    if usage_raw:
        usage = {
            "input_tokens": usage_raw.get("input_tokens", 0),
            "output_tokens": usage_raw.get("output_tokens", 0),
            "cache_read_tokens": usage_raw.get("cache_read_input_tokens"),
            "cache_write_tokens": usage_raw.get("cache_creation_input_tokens"),
        }

    event: dict[str, Any] = {
        "timestamp": timestamp,
        "type": "assistant_message",
        "event_id": event_id,
        "source": _SOURCE,
        "role": "assistant",
        "model": model,
        "text": "\n".join(text_parts),
        "tool_calls": tool_calls,
        "stop_reason": stop_reason,
        "usage": usage,
        "message_uuid": uuid,
    }
    existing_event_ids.add(event_id)
    new_events.append((timestamp, event))


def _parse_user_message(
    raw: dict[str, Any],
    uuid: str,
    timestamp: str,
    existing_event_ids: set[str],
    tool_name_by_call_id: dict[str, str],
    new_events: list[tuple[str, dict[str, Any]]],
) -> None:
    message: dict[str, Any] = raw.get("message", {})
    content = message.get("content")

    # Emit user text message if there is actual user text
    if not _has_tool_results_only(content):
        event_id = _make_event_id(uuid, "user")
        if event_id not in existing_event_ids:
            text = _extract_text_content(content)
            if text:
                event: dict[str, Any] = {
                    "timestamp": timestamp,
                    "type": "user_message",
                    "event_id": event_id,
                    "source": _SOURCE,
                    "role": "user",
                    "content": text,
                    "message_uuid": uuid,
                }
                existing_event_ids.add(event_id)
                new_events.append((timestamp, event))

    # Emit tool result events for any tool_result blocks
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            tool_call_id: str = block.get("tool_use_id", "")
            if not tool_call_id:
                continue

            event_id = _make_event_id(uuid, f"tool_result-{tool_call_id}")
            if event_id in existing_event_ids:
                continue

            # Extract output text
            result_content = block.get("content", "")
            if isinstance(result_content, list):
                parts: list[str] = []
                for item in result_content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(item.get("text", ""))
                    elif isinstance(item, str):
                        parts.append(item)
                result_content = "\n".join(parts)
            elif not isinstance(result_content, str):
                result_content = str(result_content)

            if len(result_content) > _MAX_OUTPUT_LENGTH:
                result_content = result_content[:_MAX_OUTPUT_LENGTH] + "..."

            tool_name = tool_name_by_call_id.get(tool_call_id, "unknown")

            event = {
                "timestamp": timestamp,
                "type": "tool_result",
                "event_id": event_id,
                "source": _SOURCE,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "output": result_content,
                "is_error": bool(block.get("is_error", False)),
                "message_uuid": uuid,
            }
            existing_event_ids.add(event_id)
            new_events.append((timestamp, event))
