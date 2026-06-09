#!/usr/bin/env bash
# Common transcript converter for opencode agents.
#
# Reads the raw opencode transcript at logs/opencode_transcript/events.jsonl
# (written in-process by the OpenCode plugin: one line per message.updated /
# message.part.updated event, as {"type":..., "properties":...}) and converts
# it into the agent-agnostic common format at
# events/opencode/common_transcript/events.jsonl (what `mngr transcript` reads).
#
# OpenCode's data model (verified against the @opencode-ai/sdk 1.16.2 types):
#   message.updated  -> {"info": Message}    Message = {id, sessionID, role:
#                       "user"|"assistant", time:{created,...}, providerID?,
#                       modelID?, ...}
#   message.part.updated -> {"part": Part}    Part has a `type` discriminator:
#                       "text"   {text}
#                       "tool"   {callID, tool, state:{status, input, output?,
#                                 error?, ...}}  (status: pending|running|
#                                 completed|error)
#                       plus reasoning/step-start/... (dropped).
#
# Parts stream in place (the same part id is updated repeatedly as text grows
# and tool state advances pending->running->completed). So unlike the
# antigravity converter (which appends + dedups immutable per-step events),
# this converter REPROCESSES the whole raw file each pass, taking the latest
# state of each message/part, and atomically rewrites the output file. That way
# a partially-streamed assistant message resolves to its final text instead of
# being frozen at the first snapshot.
#
# Emits:
#   user message text parts        -> user_message
#   assistant message text + tools -> assistant_message (tool_calls[])
#   completed/errored tool parts   -> tool_result (paired by callID)
#
# Usage: opencode_common_transcript.sh [--single-pass]
#
# Environment:
#   MNGR_AGENT_STATE_DIR  - agent state directory (contains events/, logs/)

set -euo pipefail

AGENT_DATA_DIR="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR must be set}"
INPUT_FILE="$AGENT_DATA_DIR/logs/opencode_transcript/events.jsonl"
OUTPUT_FILE="$AGENT_DATA_DIR/events/opencode/common_transcript/events.jsonl"
POLL_INTERVAL=5

_MNGR_LOG_TYPE="opencode_common_transcript"
_MNGR_LOG_SOURCE="logs/opencode_common_transcript"
_MNGR_LOG_FILE="$AGENT_DATA_DIR/events/logs/opencode_common_transcript/events.jsonl"
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
import time

_MAX_INPUT_PREVIEW_LENGTH = 200
_MAX_OUTPUT_LENGTH = 2000
_SOURCE = "opencode/common_transcript"


def _truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _short_value(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))


def _iso_from_ms(created_ms):
    if not isinstance(created_ms, (int, float)):
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created_ms / 1000.0))


def _message_text(parts):
    chunks = []
    for part in parts:
        if part.get("type") == "text" and not part.get("synthetic"):
            text = part.get("text")
            if isinstance(text, str) and text:
                chunks.append(text)
    return "".join(chunks)


def _tool_parts(parts):
    return [part for part in parts if part.get("type") == "tool"]


def _tool_state_output(state):
    if not isinstance(state, dict):
        return "", False
    status = state.get("status", "")
    if status == "error":
        return _short_value(state.get("error", "")), True
    return _short_value(state.get("output", "")), False


def convert():
    input_file = os.environ["_INPUT_FILE"]
    output_file = os.environ["_OUTPUT_FILE"]

    # latest message info by id, and ordered (first-seen) message ids
    message_by_id = {}
    message_order = []
    # latest part by id, ordered part ids per message id (first-seen)
    part_by_id = {}
    part_order_by_message = {}

    with open(input_file) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            props = event.get("properties")
            if not isinstance(props, dict):
                continue

            if event_type == "message.updated":
                info = props.get("info")
                if not isinstance(info, dict) or "id" not in info:
                    continue
                message_id = info["id"]
                if message_id not in message_by_id:
                    message_order.append(message_id)
                message_by_id[message_id] = info

            elif event_type == "message.part.updated":
                part = props.get("part")
                if not isinstance(part, dict) or "id" not in part or "messageID" not in part:
                    continue
                part_id = part["id"]
                message_id = part["messageID"]
                if part_id not in part_by_id:
                    part_order_by_message.setdefault(message_id, []).append(part_id)
                part_by_id[part_id] = part

    events = []
    for message_id in sorted(message_order, key=lambda mid: message_by_id[mid].get("time", {}).get("created", 0)):
        info = message_by_id[message_id]
        role = info.get("role", "")
        session_id = info.get("sessionID", "")
        timestamp = _iso_from_ms(info.get("time", {}).get("created"))
        parts = [part_by_id[pid] for pid in part_order_by_message.get(message_id, []) if pid in part_by_id]
        text = _message_text(parts)
        tool_parts = _tool_parts(parts)

        if role == "user":
            if not text:
                continue
            events.append({
                "timestamp": timestamp,
                "type": "user_message",
                "event_id": message_id + "-user",
                "source": _SOURCE,
                "role": "user",
                "content": text,
                "conversation_id": session_id,
                "message_id": message_id,
            })
            continue

        if role != "assistant":
            continue

        tool_calls = []
        for part in tool_parts:
            call_id = part.get("callID", "")
            tool_name = part.get("tool", "")
            state = part.get("state")
            tool_input = state.get("input", {}) if isinstance(state, dict) else {}
            tool_calls.append({
                "tool_call_id": call_id,
                "tool_name": tool_name,
                "input_preview": _truncate(_short_value(tool_input), _MAX_INPUT_PREVIEW_LENGTH),
            })

        provider_id = info.get("providerID", "")
        model_id = info.get("modelID", "")
        model = f"{provider_id}/{model_id}" if provider_id and model_id else None

        events.append({
            "timestamp": timestamp,
            "type": "assistant_message",
            "event_id": message_id + "-assistant",
            "source": _SOURCE,
            "role": "assistant",
            "model": model,
            "text": text,
            "tool_calls": tool_calls,
            "stop_reason": info.get("finish"),
            "usage": None,
            "conversation_id": session_id,
            "message_id": message_id,
        })

        for part in tool_parts:
            state = part.get("state")
            status = state.get("status", "") if isinstance(state, dict) else ""
            if status not in ("completed", "error"):
                continue
            output, is_error = _tool_state_output(state)
            events.append({
                "timestamp": timestamp,
                "type": "tool_result",
                "event_id": part["id"] + "-tool_result",
                "source": _SOURCE,
                "tool_call_id": part.get("callID", ""),
                "tool_name": part.get("tool", ""),
                "output": _truncate(output, _MAX_OUTPUT_LENGTH),
                "is_error": is_error,
                "conversation_id": session_id,
                "message_id": message_id,
            })

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    tmp_path = output_file + ".tmp"
    with open(tmp_path, "w") as handle:
        for event in events:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")
    os.replace(tmp_path, output_file)
    print(str(len(events)))


convert()
CONVERT_SCRIPT
)

    if [ -s "$convert_stderr" ]; then
        log_warn "convert error: $(cat "$convert_stderr")"
        cat "$convert_stderr" >&2
    fi
    rm -f "$convert_stderr"

    local converted="${result:-0}"
    if [ "$converted" -gt 0 ] 2>/dev/null; then
        log_debug "Wrote $converted common event(s) -> events/opencode/common_transcript/events.jsonl"
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
