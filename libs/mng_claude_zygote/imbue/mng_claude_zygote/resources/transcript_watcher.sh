#!/bin/bash
# Transcript watcher for changeling agents.
#
# Converts raw Claude Code transcript events from
# logs/claude_transcript/events.jsonl into a common, agent-agnostic
# format at events/common_transcript/events.jsonl.
#
# The common format focuses on semantically important messages (user input,
# assistant output, tool calls, tool results) and drops noise like progress
# events, file-history snapshots, and system bookkeeping.
#
# Each output line is a self-describing JSON object with the standard event
# envelope (timestamp, type, event_id, source) plus message-specific fields.
#
# The watcher uses an ID-based dedup strategy: each output event_id is
# derived from the source event's uuid, so re-processing the same input
# never produces duplicate output. The input file is append-only (populated
# by stream_transcript.sh which watches all session files).
#
# Usage: transcript_watcher.sh
#
# Environment:
#   MNG_AGENT_STATE_DIR  - agent state directory (contains events/)
#   MNG_HOST_DIR         - host data directory (contains events/ for log output)

set -euo pipefail

AGENT_DATA_DIR="${MNG_AGENT_STATE_DIR:?MNG_AGENT_STATE_DIR must be set}"
HOST_DIR="${MNG_HOST_DIR:?MNG_HOST_DIR must be set}"
INPUT_FILE="$AGENT_DATA_DIR/logs/claude_transcript/events.jsonl"
OUTPUT_FILE="$AGENT_DATA_DIR/events/common_transcript/events.jsonl"
LOG_FILE="$HOST_DIR/events/logs/transcript_watcher/events.jsonl"

# Read poll interval from settings.toml, fall back to default
mkdir -p "$(dirname "$LOG_FILE")"
POLL_INTERVAL=$(python3 -c "
import tomllib, pathlib, sys
p = pathlib.Path('${MNG_AGENT_STATE_DIR}/settings.toml')
try:
    s = tomllib.loads(p.read_text()) if p.exists() else {}
    print(s.get('watchers', {}).get('transcript_poll_interval_seconds', 5))
except Exception as e:
    print(f'WARNING: failed to load settings: {e}', file=sys.stderr)
    print(5)
" 2>>"$LOG_FILE" || echo 5)

log() {
    local ts
    ts=$(date -u +"%Y-%m-%dT%H:%M:%S.%NZ")
    local msg="[$ts] $*"
    echo "$msg"
    mkdir -p "$(dirname "$LOG_FILE")"
    echo "$msg" >> "$LOG_FILE"
}

log_warn() {
    local ts
    ts=$(date -u +"%Y-%m-%dT%H:%M:%S.%NZ")
    echo "[$ts] WARNING: $*"
    mkdir -p "$(dirname "$LOG_FILE")"
    echo "[$ts] WARNING: $*" >> "$LOG_FILE"
}

log_debug() {
    local ts
    ts=$(date -u +"%Y-%m-%dT%H:%M:%S.%NZ")
    mkdir -p "$(dirname "$LOG_FILE")"
    echo "[$ts] [debug] $*" >> "$LOG_FILE"
}

# Convert new Claude transcript events to the common format.
#
# Reads the full input file and the set of event_ids already in the output
# file, then appends any new events whose IDs are not yet present. The
# ID-based dedup ensures correctness even if the input file is replayed.
convert_new_events() {
    if [ ! -f "$INPUT_FILE" ]; then
        log_debug "Input file not found: $INPUT_FILE"
        return
    fi

    local convert_stderr
    convert_stderr=$(mktemp)
    local result
    result=$(_INPUT_FILE="$INPUT_FILE" \
             _OUTPUT_FILE="$OUTPUT_FILE" \
             python3 << 'CONVERT_SCRIPT' 2>"$convert_stderr" || true
import json
import os
import sys


# Maximum length for tool input preview and tool output
_MAX_INPUT_PREVIEW_LENGTH = 200
_MAX_OUTPUT_LENGTH = 2000


def _extract_text_content(content):
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


def _has_tool_results_only(content):
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


def _make_event_id(uuid, suffix):
    """Derive a deterministic event_id from the source UUID and a suffix."""
    return f"{uuid}-{suffix}"


def convert():
    input_file = os.environ["_INPUT_FILE"]
    output_file = os.environ["_OUTPUT_FILE"]

    # Collect existing event IDs from the output file for dedup
    existing_ids = set()
    if os.path.isfile(output_file):
        with open(output_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    existing_ids.add(json.loads(line)["event_id"])
                except (json.JSONDecodeError, KeyError):
                    continue

    if not os.path.isfile(input_file):
        print("0")
        return

    # Track tool_use_id -> tool_name from assistant messages so we can
    # label tool results with the correct tool name
    tool_name_by_call_id = {}

    new_events = []

    with open(input_file) as f:
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
                text_parts = []
                tool_calls = []
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

                        tool_calls.append({
                            "tool_call_id": call_id,
                            "tool_name": tool_name,
                            "input_preview": input_preview,
                        })

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

    if not new_events:
        print("0")
        return

    # Sort by timestamp and append to the output file
    new_events.sort(key=lambda x: x[0])

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "a") as f:
        for _, event in new_events:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")

    print(str(len(new_events)))


convert()
CONVERT_SCRIPT
)

    if [ -s "$convert_stderr" ]; then
        log_warn "convert error: $(cat "$convert_stderr")"
    fi
    rm -f "$convert_stderr"

    local converted="${result:-0}"
    if [ "$converted" -gt 0 ] 2>/dev/null; then
        log "Converted $converted new event(s) -> events/common_transcript/events.jsonl"
    else
        log_debug "No new events to convert"
    fi
}

main() {
    mkdir -p "$(dirname "$OUTPUT_FILE")"

    log "Transcript watcher started"
    log "  Agent data dir: $AGENT_DATA_DIR"
    log "  Input: $INPUT_FILE"
    log "  Output: $OUTPUT_FILE"
    log "  Log file: $LOG_FILE"
    log "  Poll interval: ${POLL_INTERVAL}s"

    if command -v inotifywait &>/dev/null && [ -f "$INPUT_FILE" ]; then
        log "Using inotifywait for file watching"
        while true; do
            inotifywait -q -t "$POLL_INTERVAL" -e modify,create "$INPUT_FILE" 2>/dev/null || true
            convert_new_events
        done
    else
        if ! command -v inotifywait &>/dev/null; then
            log "inotifywait not available, using polling"
        elif [ ! -f "$INPUT_FILE" ]; then
            log "Input file not yet created at $INPUT_FILE, using polling"
        fi
        while true; do
            convert_new_events
            sleep "$POLL_INTERVAL"
        done
    fi
}

main
