#!/usr/bin/env python3
"""Common-transcript converter for claude agents (invoked by common_transcript.sh).

Reads the raw Claude transcript (``logs/claude_transcript/events.jsonl``,
produced by stream_transcript.sh) and appends semantically important events
(user input, assistant output, tool calls, tool results) in the common,
agent-agnostic format to ``events/claude/common_transcript/events.jsonl``. Noise
(progress events, file-history snapshots, system bookkeeping) is dropped.

Dedup is ID-based: each output ``event_id`` is derived from the source event's
uuid, so re-processing the same input never produces duplicate output.

Invoked as ``python3 common_transcript_convert.py`` with the input/output paths
passed via the ``_INPUT_FILE`` / ``_OUTPUT_FILE`` environment variables that
common_transcript.sh sets. Malformed or null lines are dropped silently; only an
uncaught exception writes to stderr, which the shell reports as a convert error
(the count of appended events is printed to stdout for common_transcript.sh to
capture). Split out of
the shell script (it used to be an inline ``python3`` heredoc) so the logic is
lintable, type-checked, and unit-testable directly rather than only through a
subprocess.
"""

from __future__ import annotations

import json
import os
from typing import Any
from typing import Union

# A parsed-JSON value of unspecified shape. Stdlib-only (pydantic isn't importable
# under the host's bare python3). Spelled with Union, not ``|``: this assignment runs
# at import, and ``|`` on types needs python 3.10+. noqa stops ruff rewriting it.
JsonValue = Union[str, int, float, bool, None, list, dict]  # noqa: UP007

# Maximum length for tool input preview and tool output
_MAX_INPUT_PREVIEW_LENGTH = 200
_MAX_OUTPUT_LENGTH = 2000

# Claude Code marks framework-injected user messages (stop hook output,
# local-command caveats, etc.) with isMeta=true on the top-level event. We
# use that flag to reclassify those messages as tool results so transcript
# viewers show them under the tool role rather than the user role (no human
# typed them).


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _extract_text_content(content: JsonValue) -> str:
    """Extract plain text from a message content field (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if text:
                parts.append(text)
    return "\n".join(parts)


def _has_tool_results_only(content: JsonValue) -> bool:
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
        else:
            # Unknown block shape (not a dict or str): ignore it, as the original
            # converter did -- it neither confirms nor denies tool-results-only.
            continue
    return True


def _make_event_id(uuid: str, suffix: str) -> str:
    """Derive a deterministic event_id from the source UUID and a suffix."""
    return f"{uuid}-{suffix}"


def _load_existing_ids(output_file: str) -> set[str]:
    ids: set[str] = set()
    if not os.path.isfile(output_file):
        return ids
    with open(output_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(json.loads(line)["event_id"])
            except (json.JSONDecodeError, KeyError):
                continue
    return ids


def convert(input_file: str, output_file: str) -> int:
    """Append new common-transcript events from ``input_file`` to ``output_file``; return the count."""
    existing_ids = _load_existing_ids(output_file)

    if not os.path.isfile(input_file):
        return 0

    # Track tool_use_id -> tool_name from assistant messages so we can
    # label tool results with the correct tool name
    tool_name_by_call_id: dict[str, str] = {}

    new_events: list[tuple[str, dict[str, Any]]] = []

    with open(input_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = raw.get("type", "")
            uuid = raw.get("uuid", "")
            timestamp = raw.get("timestamp", "")
            is_meta = bool(raw.get("isMeta", False))

            if not uuid or not timestamp:
                continue

            # -- assistant messages --
            if event_type == "assistant":
                event_id = _make_event_id(uuid, "assistant")
                if event_id in existing_ids:
                    continue

                raw_message = raw.get("message")
                if not isinstance(raw_message, dict):
                    # A null/missing message carries no usable content -- drop the
                    # line rather than emit an empty event or crash.
                    continue
                message = raw_message
                content_blocks = message.get("content", [])
                model = message.get("model", "unknown")
                stop_reason = message.get("stop_reason")
                usage_raw = message.get("usage", {})

                # Extract text and tool calls. ``parts`` preserves the original
                # interleaving order of text and tool_use blocks; the flat
                # ``text``/``tool_calls`` remain the baseline every emitter fills.
                text_parts = []
                tool_calls = []
                parts: list[dict[str, Any]] = []
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type", "")
                    if block_type == "text":
                        text = block.get("text", "")
                        if text:
                            text_parts.append(text)
                            parts.append({"type": "text", "content": text})
                    elif block_type == "tool_use":
                        call_id = block.get("id", "")
                        tool_name = block.get("name", "")
                        tool_input = block.get("input", {})
                        input_preview = _truncate(
                            json.dumps(tool_input, separators=(",", ":")), _MAX_INPUT_PREVIEW_LENGTH
                        )

                        # Track for tool result labeling
                        if call_id and tool_name:
                            tool_name_by_call_id[call_id] = tool_name

                        tool_call = {
                            "tool_call_id": call_id,
                            "tool_name": tool_name,
                            "input_preview": input_preview,
                        }
                        tool_calls.append(tool_call)
                        parts.append({"type": "tool_call", **tool_call})
                    else:
                        # Other block types (thinking, redacted_thinking, etc.)
                        # carry no transcript-visible text or tool call.
                        continue

                # Build usage
                usage = None
                if usage_raw:
                    usage = {
                        "input_tokens": usage_raw.get("input_tokens", 0),
                        "output_tokens": usage_raw.get("output_tokens", 0),
                        "cache_read_tokens": usage_raw.get("cache_read_input_tokens"),
                        "cache_write_tokens": usage_raw.get("cache_creation_input_tokens"),
                    }

                event = {
                    "timestamp": timestamp,
                    "type": "assistant_message",
                    "event_id": event_id,
                    "source": "claude/common_transcript",
                    "role": "assistant",
                    "model": model,
                    "text": "\n".join(text_parts),
                    "tool_calls": tool_calls,
                    "parts": parts,
                    "parts_ordered": True,
                    "finish_reason": stop_reason,
                    "usage": usage,
                    "message_uuid": uuid,
                }
                new_events.append((timestamp, event))

            # -- user messages (may contain text, tool results, or both) --
            elif event_type == "user":
                raw_message = raw.get("message")
                if not isinstance(raw_message, dict):
                    # A null/missing message carries no usable content -- drop the
                    # line rather than emit an empty event or crash.
                    continue
                message = raw_message
                content = message.get("content")

                # Emit user text message if there is actual user text
                if not _has_tool_results_only(content):
                    text = _extract_text_content(content)
                    if is_meta:
                        # Framework-injected message (stop hook output, etc.) --
                        # reclassify as tool_result so it doesn't masquerade as user input.
                        event_id = _make_event_id(uuid, "meta")
                        if event_id not in existing_ids and text:
                            output = _truncate(text, _MAX_OUTPUT_LENGTH)
                            event = {
                                "timestamp": timestamp,
                                "type": "tool_result",
                                "event_id": event_id,
                                "source": "claude/common_transcript",
                                "tool_call_id": f"meta-{uuid}",
                                "tool_name": "meta",
                                "output": output,
                                "is_error": False,
                                "message_uuid": uuid,
                            }
                            new_events.append((timestamp, event))
                    else:
                        event_id = _make_event_id(uuid, "user")
                        if event_id not in existing_ids:
                            if text:
                                event = {
                                    "timestamp": timestamp,
                                    "type": "user_message",
                                    "event_id": event_id,
                                    "source": "claude/common_transcript",
                                    "role": "user",
                                    "content": text,
                                    "message_uuid": uuid,
                                }
                                new_events.append((timestamp, event))

                # Emit tool result events for any tool_result blocks
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_result":
                            continue
                        tool_call_id = block.get("tool_use_id", "")
                        if not tool_call_id:
                            continue

                        event_id = _make_event_id(uuid, f"tool_result-{tool_call_id}")
                        if event_id in existing_ids:
                            continue

                        # Normalize the raw result content to a single output string.
                        raw_result = block.get("content", "")
                        if isinstance(raw_result, list):
                            parts = []
                            for item in raw_result:
                                if isinstance(item, dict) and item.get("type") == "text":
                                    parts.append(item.get("text", ""))
                                elif isinstance(item, str):
                                    parts.append(item)
                                else:
                                    # Non-text result block (image, etc.): no text to extract.
                                    continue
                            output_text = "\n".join(parts)
                        elif isinstance(raw_result, str):
                            output_text = raw_result
                        else:
                            output_text = str(raw_result)

                        output_text = _truncate(output_text, _MAX_OUTPUT_LENGTH)

                        tool_name = tool_name_by_call_id.get(tool_call_id, "unknown")

                        event = {
                            "timestamp": timestamp,
                            "type": "tool_result",
                            "event_id": event_id,
                            "source": "claude/common_transcript",
                            "tool_call_id": tool_call_id,
                            "tool_name": tool_name,
                            "output": output_text,
                            "is_error": bool(block.get("is_error", False)),
                            "message_uuid": uuid,
                        }
                        new_events.append((timestamp, event))

            else:
                # Noise: progress, file-history-snapshot, system, result, etc.
                continue

    if not new_events:
        return 0

    # Sort by timestamp and append to the output file
    new_events.sort(key=lambda x: x[0])

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "a", encoding="utf-8") as f:
        for _, event in new_events:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")

    return len(new_events)


if __name__ == "__main__":
    print(convert(os.environ["_INPUT_FILE"], os.environ["_OUTPUT_FILE"]))
