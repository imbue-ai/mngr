#!/usr/bin/env bash
# Common transcript converter for gemini agents.
#
# Reads the raw gemini transcript at logs/gemini_transcript/events.jsonl
# (produced by stream_transcript.sh) and converts semantically important
# events (user input, model output, tool calls, tool results) into a common,
# agent-agnostic format at events/gemini/common_transcript/events.jsonl.
#
# Noise like session-start headers and $set lastUpdated bookkeeping is dropped.
#
# Each output line is a self-describing JSON object with the standard event
# envelope (timestamp, type, event_id, source) plus message-specific fields.
# Event ids are derived deterministically from each source message id, so
# re-processing the same input never produces duplicate output.
#
# Usage: common_transcript.sh [--single-pass]
#
# Environment:
#   MNGR_AGENT_STATE_DIR  - agent state directory (contains events/, logs/)

set -euo pipefail

AGENT_DATA_DIR="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR must be set}"
INPUT_FILE="$AGENT_DATA_DIR/logs/gemini_transcript/events.jsonl"
OUTPUT_FILE="$AGENT_DATA_DIR/events/gemini/common_transcript/events.jsonl"
POLL_INTERVAL=5

# Configure and source the shared logging library
_MNGR_LOG_TYPE="common_transcript"
_MNGR_LOG_SOURCE="logs/common_transcript"
_MNGR_LOG_FILE="$AGENT_DATA_DIR/events/logs/common_transcript/events.jsonl"
# shellcheck source=mngr_log.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_log.sh"

# Convert new gemini transcript events to the common format.
#
# Reads the raw transcript stream (produced by stream_transcript.sh) and the
# set of event_ids already in the output file, then appends any new events
# whose IDs are not yet present. The ID-based dedup ensures correctness even
# if the input is replayed.
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


# Maximum length for tool input preview and tool output
_MAX_INPUT_PREVIEW_LENGTH = 200
_MAX_OUTPUT_LENGTH = 2000


def _extract_text(content):
    """Extract plain text from a gemini message content field (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text", "")
                if text:
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _extract_tool_output(result):
    """Concatenate the `output` strings from a gemini tool call result array."""
    if not isinstance(result, list):
        return ""
    parts = []
    for item in result:
        if not isinstance(item, dict):
            continue
        func_resp = item.get("functionResponse")
        if isinstance(func_resp, dict):
            response = func_resp.get("response", {})
            if isinstance(response, dict):
                output = response.get("output", "")
                if isinstance(output, str):
                    parts.append(output)
                else:
                    parts.append(json.dumps(output))
    return "\n".join(parts)


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
            uuid = raw.get("id", "")
            timestamp = raw.get("timestamp", "")

            # Skip session header ($set updates, kind=main entries with no id/type)
            if not uuid or not timestamp:
                continue

            if event_type == "user":
                event_id = f"{uuid}-user"
                if event_id in existing_ids:
                    continue
                text = _extract_text(raw.get("content"))
                if not text:
                    continue
                event = {
                    "timestamp": timestamp,
                    "type": "user_message",
                    "event_id": event_id,
                    "source": "gemini/common_transcript",
                    "role": "user",
                    "content": text,
                    "message_uuid": uuid,
                }
                new_events.append((timestamp, event))

            elif event_type == "gemini":
                text = _extract_text(raw.get("content"))
                tokens_raw = raw.get("tokens", {})
                usage = None
                if isinstance(tokens_raw, dict) and tokens_raw:
                    usage = {
                        "input_tokens": tokens_raw.get("input", 0),
                        "output_tokens": tokens_raw.get("output", 0),
                        "cache_read_tokens": tokens_raw.get("cached"),
                        "cache_write_tokens": None,
                    }

                raw_tool_calls = raw.get("toolCalls", [])
                if not isinstance(raw_tool_calls, list):
                    raw_tool_calls = []

                tool_calls = []
                for tc in raw_tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    call_id = tc.get("id", "")
                    tool_name = tc.get("name", "")
                    args = tc.get("args", {})
                    input_preview = json.dumps(args, separators=(",", ":"))
                    if len(input_preview) > _MAX_INPUT_PREVIEW_LENGTH:
                        input_preview = input_preview[:_MAX_INPUT_PREVIEW_LENGTH] + "..."
                    tool_calls.append({
                        "tool_call_id": call_id,
                        "tool_name": tool_name,
                        "input_preview": input_preview,
                    })

                event_id = f"{uuid}-assistant"
                if event_id not in existing_ids:
                    event = {
                        "timestamp": timestamp,
                        "type": "assistant_message",
                        "event_id": event_id,
                        "source": "gemini/common_transcript",
                        "role": "assistant",
                        "model": raw.get("model", "unknown"),
                        "text": text,
                        "tool_calls": tool_calls,
                        "stop_reason": None,
                        "usage": usage,
                        "message_uuid": uuid,
                    }
                    new_events.append((timestamp, event))

                for tc in raw_tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    call_id = tc.get("id", "")
                    if not call_id:
                        continue
                    tr_event_id = f"{uuid}-tool_result-{call_id}"
                    if tr_event_id in existing_ids:
                        continue
                    output = _extract_tool_output(tc.get("result"))
                    if len(output) > _MAX_OUTPUT_LENGTH:
                        output = output[:_MAX_OUTPUT_LENGTH] + "..."
                    tr_timestamp = tc.get("timestamp", timestamp)
                    event = {
                        "timestamp": tr_timestamp,
                        "type": "tool_result",
                        "event_id": tr_event_id,
                        "source": "gemini/common_transcript",
                        "tool_call_id": call_id,
                        "tool_name": tc.get("name", "unknown"),
                        "output": output,
                        "is_error": tc.get("status", "success") != "success",
                        "message_uuid": uuid,
                    }
                    new_events.append((tr_timestamp, event))

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
        log_info "Converted $converted new event(s) -> events/gemini/common_transcript/events.jsonl"
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
