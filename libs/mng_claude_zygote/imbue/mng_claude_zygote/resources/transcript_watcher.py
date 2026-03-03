#!/usr/bin/env python3
"""Transcript watcher for changeling agents.

Converts raw Claude Code transcript events from
events/claude_transcript/events.jsonl into a common, agent-agnostic
format at events/common_transcript/events.jsonl.

The common format focuses on semantically important messages (user input,
assistant output, tool calls, tool results) and drops noise like progress
events, file-history snapshots, and system bookkeeping.

Each output line is a self-describing JSON object with the standard event
envelope (timestamp, type, event_id, source) plus message-specific fields.

The watcher uses an ID-based dedup strategy: each output event_id is
derived from the source event's uuid, so re-processing the same input
(which happens every 15s when claude_background_tasks.sh rewrites the
full file) never produces duplicate output.

Usage: python3 transcript_watcher.py

Environment:
  MNG_AGENT_STATE_DIR  - agent state directory (contains events/)
  MNG_HOST_DIR         - host data directory (contains events/ for event and log output)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from imbue.mng_claude_zygote.resources.watcher_common import load_watchers_section
    from imbue.mng_claude_zygote.resources.watcher_common import read_event_ids_from_jsonl
    from imbue.mng_claude_zygote.resources.watcher_common import require_env
    from imbue.mng_claude_zygote.resources.watcher_common import run_watcher_loop
    from imbue.mng_claude_zygote.resources.watcher_common import setup_watcher_logging
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from watcher_common import load_watchers_section  # type: ignore[no-redef]
    from watcher_common import read_event_ids_from_jsonl  # type: ignore[no-redef]
    from watcher_common import require_env  # type: ignore[no-redef]
    from watcher_common import run_watcher_loop  # type: ignore[no-redef]
    from watcher_common import setup_watcher_logging  # type: ignore[no-redef]

# Maximum length for tool input preview and tool output
_MAX_INPUT_PREVIEW_LENGTH = 200
_MAX_OUTPUT_LENGTH = 2000


def _load_poll_interval(agent_work_dir: Path) -> int:
    """Load transcript watcher poll interval from settings.toml."""
    watchers = load_watchers_section(agent_work_dir)
    return watchers.get("transcript_poll_interval_seconds", 5)


def _extract_text_content(content: str | list[dict[str, Any]]) -> str:
    """Extract plain text from a message content field (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = str(block.get("text", ""))
            if text:
                parts.append(text)
    return "\n".join(parts)


def _has_tool_results_only(content: str | list[dict[str, Any]]) -> bool:
    """Check if a content list contains only tool_result blocks (no user text)."""
    if isinstance(content, str):
        return False
    if not isinstance(content, list):
        return True
    for block in content:
        if isinstance(block, str):
            return False
        if isinstance(block, dict):
            block_type = str(block.get("type", ""))
            if block_type not in ("tool_result",):
                return False
    return True


def _make_event_id(uuid: str, suffix: str) -> str:
    """Derive a deterministic event_id from the source UUID and a suffix."""
    return f"{uuid}-{suffix}"


def _convert_new_events(
    input_file: Path,
    output_file: Path,
) -> int:
    """Convert new Claude transcript events to the common format.

    Reads the full input file and the set of event_ids already in the output
    file, then appends any new events whose IDs are not yet present. This
    handles the fact that claude_background_tasks.sh rewrites the input file
    from scratch every 15 seconds.

    Returns the number of new events converted.
    """
    if not input_file.is_file():
        logger.debug("Input file not found: {}", input_file)
        return 0

    existing_ids = read_event_ids_from_jsonl(output_file)

    # Track tool_use_id -> tool_name from assistant messages so we can
    # label tool results with the correct tool name
    tool_name_by_call_id: dict[str, str] = {}

    new_events: list[tuple[str, dict[str, object]]] = []

    try:
        with input_file.open() as f:
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

                if not uuid or not timestamp:
                    continue

                # -- assistant messages --
                if event_type == "assistant":
                    event_id = _make_event_id(uuid, "assistant")
                    if event_id in existing_ids:
                        continue

                    message = raw.get("message", {})
                    content_blocks = message.get("content", [])
                    model = message.get("model", "unknown")
                    stop_reason = message.get("stop_reason")
                    usage_raw = message.get("usage", {})

                    # Extract text, tool calls
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
                            call_id = block.get("id", "")
                            tool_name = block.get("name", "")
                            tool_input = block.get("input", {})
                            input_preview = json.dumps(tool_input, separators=(",", ":"))
                            if len(input_preview) > _MAX_INPUT_PREVIEW_LENGTH:
                                input_preview = input_preview[:_MAX_INPUT_PREVIEW_LENGTH] + "..."

                            # Track for tool result labeling
                            if call_id and tool_name:
                                tool_name_by_call_id[call_id] = tool_name

                            tool_calls.append(
                                {
                                    "tool_call_id": call_id,
                                    "tool_name": tool_name,
                                    "input_preview": input_preview,
                                }
                            )

                    # Build usage
                    usage = None
                    if usage_raw:
                        usage = {
                            "input_tokens": usage_raw.get("input_tokens", 0),
                            "output_tokens": usage_raw.get("output_tokens", 0),
                            "cache_read_tokens": usage_raw.get("cache_read_input_tokens"),
                            "cache_write_tokens": usage_raw.get("cache_creation_input_tokens"),
                        }

                    event: dict[str, object] = {
                        "timestamp": timestamp,
                        "type": "assistant_message",
                        "event_id": event_id,
                        "source": "common_transcript",
                        "role": "assistant",
                        "model": model,
                        "text": "\n".join(text_parts),
                        "tool_calls": tool_calls,
                        "stop_reason": stop_reason,
                        "usage": usage,
                        "message_uuid": uuid,
                    }
                    new_events.append((timestamp, event))

                # -- user messages (may contain text, tool results, or both) --
                elif event_type == "user":
                    message = raw.get("message", {})
                    content = message.get("content")

                    # Emit user text message if there is actual user text
                    if not _has_tool_results_only(content):
                        event_id = _make_event_id(uuid, "user")
                        if event_id not in existing_ids:
                            text = _extract_text_content(content)
                            if text:
                                event = {
                                    "timestamp": timestamp,
                                    "type": "user_message",
                                    "event_id": event_id,
                                    "source": "common_transcript",
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

                            # Extract output text
                            result_content = block.get("content", "")
                            if isinstance(result_content, list):
                                parts = []
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
                                "source": "common_transcript",
                                "tool_call_id": tool_call_id,
                                "tool_name": tool_name,
                                "output": result_content,
                                "is_error": bool(block.get("is_error", False)),
                                "message_uuid": uuid,
                            }
                            new_events.append((timestamp, event))

                # Skip: progress, file-history-snapshot, system, result, etc.
    except OSError as exc:
        logger.warning("Failed to read input file: {}", exc)
        return 0

    if not new_events:
        return 0

    # Sort by timestamp and append to the output file
    new_events.sort(key=lambda x: x[0])

    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("a") as f:
        for _, event in new_events:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")

    return len(new_events)


def main() -> None:
    agent_state_dir = Path(require_env("MNG_AGENT_STATE_DIR"))
    agent_work_dir = Path(require_env("MNG_AGENT_WORK_DIR"))
    host_dir = Path(require_env("MNG_HOST_DIR"))

    input_file = agent_state_dir / "events" / "claude_transcript" / "events.jsonl"
    output_file = agent_state_dir / "events" / "common_transcript" / "events.jsonl"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    setup_watcher_logging("transcript_watcher", host_dir / "events" / "logs")

    poll_interval = _load_poll_interval(agent_work_dir)

    logger.info("Transcript watcher started")
    logger.info("  Agent data dir: {}", agent_state_dir)
    logger.info("  Input: {}", input_file)
    logger.info("  Output: {}", output_file)
    logger.info("  Poll interval: {}s", poll_interval)

    watch_paths = [input_file]

    def on_tick() -> None:
        converted_count = _convert_new_events(input_file, output_file)
        if converted_count > 0:
            logger.info("Converted {} new event(s) -> events/common_transcript/events.jsonl", converted_count)
        else:
            logger.debug("No new events to convert")

    run_watcher_loop(
        "Transcript watcher",
        poll_interval,
        watch_paths,
        is_directory_mode=False,
        on_tick=on_tick,
    )


if __name__ == "__main__":
    main()
