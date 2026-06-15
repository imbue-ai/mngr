#!/usr/bin/env bash
# Common transcript converter for codex agents.
#
# Reads the raw codex rollout stream at logs/codex_transcript/events.jsonl
# (produced verbatim by stream_transcript.sh) and converts the semantically
# important rollout items into the agent-agnostic common format at
# events/codex/common_transcript/events.jsonl.
#
# codex rollout wire shape (verified live against codex 0.64.0):
#   {"timestamp":"<ISO8601>","type":<t>,"payload":<p>}
# with the item kinds this converter cares about carried under type
# "response_item":
#   payload.type=="message", role=="user"      -> user_message
#       (text = join of payload.content[] items with type "input_text", .text)
#   payload.type=="message", role=="assistant" -> assistant_message
#       (text = join of payload.content[] items with type "output_text", .text)
#   payload.type=="function_call"              -> remembered by payload.call_id
#       (name=payload.name, arguments=payload.arguments [a raw JSON string])
#   payload.type=="function_call_output"       -> tool_result, paired by call_id
#       (output=payload.output, EITHER a string OR an array of content items)
#
# Deliberately ignored:
#   type=="event_msg"   -- display duplicates of the response items above
#                          (a user_message event_msg mirrors the response_item
#                          message); emitting them would double every message.
#   session_meta / turn_context / compacted / ghost_snapshot / token_count / ...
#                       -- bookkeeping, not conversation content.
#
# Event ids: the rollout carries no global per-line id, so we synthesize a
# stable id from the line's 1-based index in the raw input file (the stream is
# append-only, so a given line keeps its index across restarts) plus the item
# kind. Re-processing the same input therefore never produces duplicates; the
# converter also dedupes against the set of event_ids already in the output file.
# function_call/function_call_output are paired by payload.call_id.
#
# Usage: common_transcript.sh [--single-pass]
#
# Environment:
#   MNGR_AGENT_STATE_DIR  - agent state directory (contains events/, logs/)

set -euo pipefail

AGENT_DATA_DIR="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR must be set}"
INPUT_FILE="$AGENT_DATA_DIR/logs/codex_transcript/events.jsonl"
OUTPUT_FILE="$AGENT_DATA_DIR/events/codex/common_transcript/events.jsonl"
POLL_INTERVAL=5

_MNGR_LOG_TYPE="common_transcript"
_MNGR_LOG_SOURCE="logs/common_transcript"
_MNGR_LOG_FILE="$AGENT_DATA_DIR/events/logs/common_transcript/events.jsonl"
# shellcheck source=mngr_log.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_log.sh"

convert_new_events() {
    if [ ! -f "$INPUT_FILE" ]; then
        log_debug "Input file not found: $INPUT_FILE"
        return
    fi

    local convert_stderr
    convert_stderr=$(mktemp)
    local result
    result=$(_INPUT_FILE="$INPUT_FILE" _OUTPUT_FILE="$OUTPUT_FILE" python3 << 'CONVERT_SCRIPT' 2>"$convert_stderr" || true
import json
import os
import sys

_MAX_INPUT_PREVIEW_LENGTH = 200
_MAX_OUTPUT_LENGTH = 2000
_SOURCE = "codex/common_transcript"


def _truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _join_content_text(content, item_type):
    """Join the .text of payload.content[] items whose type matches item_type."""
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != item_type:
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _stringify_output(output):
    """Render function_call_output.output, which is a string OR a content array."""
    if isinstance(output, str):
        return output
    # An array of content items: join the text of each, falling back to a JSON
    # dump of any item that doesn't carry a plain .text field.
    if isinstance(output, list):
        parts = []
        for item in output:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                parts.append(json.dumps(item, separators=(",", ":")))
        return "".join(parts)
    # Anything else (a bare object/number): render it as JSON so nothing is lost.
    return json.dumps(output, separators=(",", ":"))


def _load_existing_ids(output_file):
    ids = set()
    if not os.path.isfile(output_file):
        return ids
    with open(output_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(json.loads(line)["event_id"])
            except (json.JSONDecodeError, KeyError):
                continue
    return ids


def convert():
    input_file = os.environ["_INPUT_FILE"]
    output_file = os.environ["_OUTPUT_FILE"]
    existing_ids = _load_existing_ids(output_file)
    if not os.path.isfile(input_file):
        print("0")
        return

    new_events = []
    # Pending function calls awaiting their output, keyed by call_id. Each value
    # carries the synthetic tool_call_id, the tool name, and the input preview.
    pending_call_by_id = {}

    with open(input_file) as f:
        for line_index, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue

            # Ignore event_msg entirely (display duplicates of response_items).
            if raw.get("type") != "response_item":
                continue
            payload = raw.get("payload")
            if not isinstance(payload, dict):
                continue

            timestamp = raw.get("timestamp", "")
            payload_type = payload.get("type")

            if payload_type == "message" and payload.get("role") == "user":
                event_id = f"line-{line_index}-user"
                if event_id in existing_ids:
                    continue
                text = _join_content_text(payload.get("content"), "input_text")
                # An empty user message carries no signal -> drop it.
                if not text:
                    continue
                new_events.append((timestamp, line_index, {
                    "timestamp": timestamp,
                    "type": "user_message",
                    "event_id": event_id,
                    "source": _SOURCE,
                    "role": "user",
                    "content": text,
                }))

            elif payload_type == "message" and payload.get("role") == "assistant":
                event_id = f"line-{line_index}-assistant"
                if event_id in existing_ids:
                    continue
                text = _join_content_text(payload.get("content"), "output_text")
                new_events.append((timestamp, line_index, {
                    "timestamp": timestamp,
                    "type": "assistant_message",
                    "event_id": event_id,
                    "source": _SOURCE,
                    "role": "assistant",
                    "model": None,
                    "text": text,
                    "tool_calls": [],
                    "stop_reason": None,
                    "usage": None,
                }))

            elif payload_type == "function_call":
                call_id = payload.get("call_id")
                if not isinstance(call_id, str) or not call_id:
                    continue
                name = payload.get("name", "")
                arguments = payload.get("arguments", "")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, separators=(",", ":"))
                tool_call_id = f"line-{line_index}-tc"
                pending_call_by_id[call_id] = {
                    "tool_call_id": tool_call_id,
                    "tool_name": name if isinstance(name, str) else "",
                    "input_preview": _truncate(arguments, _MAX_INPUT_PREVIEW_LENGTH),
                }

            elif payload_type == "function_call_output":
                call_id = payload.get("call_id")
                pending = pending_call_by_id.pop(call_id, None) if isinstance(call_id, str) else None
                # A function_call_output with no matching function_call has
                # nothing to pair with -> drop it.
                if pending is None:
                    continue
                event_id = f"line-{line_index}-tool_result"
                if event_id in existing_ids:
                    continue
                output = _truncate(_stringify_output(payload.get("output", "")), _MAX_OUTPUT_LENGTH)
                new_events.append((timestamp, line_index, {
                    "timestamp": timestamp,
                    "type": "tool_result",
                    "event_id": event_id,
                    "source": _SOURCE,
                    "tool_call_id": pending["tool_call_id"],
                    "tool_name": pending["tool_name"],
                    "output": output,
                    "is_error": False,
                }))

    if not new_events:
        print("0")
        return

    # Stable order: by line index (the append-only stream order), which also
    # keeps tool_results after their originating call.
    new_events.sort(key=lambda triple: triple[1])
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "a") as f:
        for _, _, event in new_events:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")

    print(str(len(new_events)))


convert()
CONVERT_SCRIPT
)

    if [ -s "$convert_stderr" ]; then
        # Forward the heredoc Python's stderr to both the structured log
        # (via log_warn) and the process's stderr -- the latter is what tests
        # and operators read when something has gone wrong with conversion.
        log_warn "convert error: $(cat "$convert_stderr")"
        cat "$convert_stderr" >&2
    fi
    rm -f "$convert_stderr"

    local converted="${result:-0}"
    if [ "$converted" -gt 0 ] 2>/dev/null; then
        log_info "Converted $converted new event(s) -> events/codex/common_transcript/events.jsonl"
    fi
}

main() {
    local is_single_pass=false
    if [ "${1:-}" = "--single-pass" ]; then
        is_single_pass=true
    fi

    mkdir -p "$(dirname "$OUTPUT_FILE")"

    log_info "Common transcript converter started"
    log_info "  Agent data dir: $AGENT_DATA_DIR"
    log_info "  Input: $INPUT_FILE"
    log_info "  Output: $OUTPUT_FILE"
    log_info "  Poll interval: ${POLL_INTERVAL}s"

    if [ "$is_single_pass" = true ]; then
        convert_new_events
        return
    fi

    while true; do
        convert_new_events
        sleep "$POLL_INTERVAL"
    done
}

main "${1:-}"
