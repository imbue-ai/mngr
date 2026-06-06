#!/usr/bin/env bash
# Common transcript converter for antigravity agents.
#
# Reads the raw antigravity transcript at
# logs/antigravity_transcript/events.jsonl (produced by stream_transcript.sh,
# with each event augmented to carry `_mngr_conv_id`) and converts
# semantically important events into the agent-agnostic common format at
# events/antigravity/common_transcript/events.jsonl.
#
# Antigravity's transcript shape (captured live against agy 1.0.0):
#   {"step_index":N, "source":<USER_EXPLICIT|MODEL|SYSTEM>,
#    "type":<USER_INPUT|PLANNER_RESPONSE|CODE_ACTION|CONVERSATION_HISTORY|...>,
#    "status":<DONE|...>, "created_at":"<ISO8601>",
#    "content":"...", "thinking":"...", "tool_calls":[{...}], ...}
#
# This converter emits:
#   USER_EXPLICIT/USER_INPUT       -> user_message  (extracted from the
#                                       <USER_REQUEST>...</USER_REQUEST>
#                                       envelope; metadata is dropped)
#   MODEL/PLANNER_RESPONSE         -> assistant_message  (any tool_calls
#                                       attached as tool_calls[])
#   MODEL/CODE_ACTION              -> tool_result (paired with the most
#                                       recent PLANNER_RESPONSE tool_call
#                                       in the same conversation)
#   SYSTEM/CONVERSATION_HISTORY    -> dropped (bookkeeping)
#   everything else                -> dropped (best-effort: forward-compat
#                                       with future agy schema additions)
#
# Tool-call ids are synthetic: agy's transcript does not carry an id on
# tool_calls (only `name` + `args`), so we mint
# "<conv_id>-<step_index>-tc<idx>" using the conversation id smuggled in
# from the streamer's `_mngr_conv_id` field. Pairing with CODE_ACTION
# uses last-seen-tool-call-in-conversation since agy emits CODE_ACTION
# immediately after the PLANNER_RESPONSE that called the tool.
#
# Event ids are derived deterministically so re-processing the same input
# never produces duplicates (the converter dedupes against the set of
# event_ids already in the output file).
#
# Usage: common_transcript.sh [--single-pass]
#
# Environment:
#   MNGR_AGENT_STATE_DIR  - agent state directory (contains events/, logs/)

set -euo pipefail

AGENT_DATA_DIR="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR must be set}"
INPUT_FILE="$AGENT_DATA_DIR/logs/antigravity_transcript/events.jsonl"
OUTPUT_FILE="$AGENT_DATA_DIR/events/antigravity/common_transcript/events.jsonl"
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
import re
import sys

_MAX_INPUT_PREVIEW_LENGTH = 200
_MAX_OUTPUT_LENGTH = 2000

# Strip Antigravity's USER_REQUEST/ADDITIONAL_METADATA/USER_SETTINGS_CHANGE
# envelope from the raw user content. We keep only the inner text the user
# actually typed; metadata about local time and model selection is noise
# for transcript consumers.
_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)


def _extract_user_text(content, conv_id, step_index):
    """Return the inner <USER_REQUEST> text, or None when the envelope is missing.

    agy 1.0.0 always wraps USER_INPUT in
    ``<USER_REQUEST>...</USER_REQUEST>\\n<ADDITIONAL_METADATA>...</ADDITIONAL_METADATA>``.
    If the envelope is absent, a silent fall-through to the raw content would
    bake agy's bookkeeping (local-time / model-selection metadata, future
    fields) into the user-facing transcript without any indication that the
    converter's contract was violated. We log loudly to stderr (the calling
    bash surfaces this as a log_warn "convert error: ...") and return None
    so the caller drops the event; the next agy version that changes the
    envelope shape will produce an obvious schema-break signal instead of
    silent garbage.
    """
    if not isinstance(content, str):
        sys.stderr.write(
            f"USER_INPUT content is not a string for conv={conv_id} step={step_index}; dropping event\n"
        )
        return None
    match = _USER_REQUEST_RE.search(content)
    if match is None:
        sys.stderr.write(
            f"USER_INPUT content missing <USER_REQUEST> envelope for conv={conv_id} step={step_index}; "
            "dropping event so the schema break is visible upstream\n"
        )
        return None
    return match.group(1)


def _short_value(value):
    """Render an arbitrary JSON value as a short string for an input preview."""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))


def _tool_call_id(conv_id, step_index, idx):
    return f"{conv_id}-{step_index}-tc{idx}"


def _truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


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
    # Track the last assistant tool call we emitted, per conversation, so
    # CODE_ACTION events can be paired with their originating tool call.
    last_tool_call_by_conv = {}

    with open(input_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue

            conv_id = raw.get("_mngr_conv_id", "")
            if not conv_id:
                continue
            step_index = raw.get("step_index")
            if step_index is None:
                continue
            timestamp = raw.get("created_at", "")
            source = raw.get("source", "")
            type_ = raw.get("type", "")

            if source == "USER_EXPLICIT" and type_ == "USER_INPUT":
                event_id = f"{conv_id}-{step_index}-user"
                if event_id in existing_ids:
                    continue
                text = _extract_user_text(raw.get("content"), conv_id, step_index)
                # _extract_user_text already returned None and logged when the
                # envelope is missing or content is not a string; an empty
                # USER_REQUEST body is also dropped as it carries no signal.
                if not text:
                    continue
                new_events.append((timestamp, {
                    "timestamp": timestamp,
                    "type": "user_message",
                    "event_id": event_id,
                    "source": "antigravity/common_transcript",
                    "role": "user",
                    "content": text,
                    "conversation_id": conv_id,
                    "step_index": step_index,
                }))

            elif source == "MODEL" and type_ == "PLANNER_RESPONSE":
                text = raw.get("content", "")
                raw_tool_calls = raw.get("tool_calls") or []
                tool_calls = []
                for idx, tc in enumerate(raw_tool_calls):
                    if not isinstance(tc, dict):
                        continue
                    name = tc.get("name", "")
                    args = tc.get("args", {})
                    input_preview = _truncate(_short_value(args), _MAX_INPUT_PREVIEW_LENGTH)
                    call_id = _tool_call_id(conv_id, step_index, idx)
                    tool_calls.append({
                        "tool_call_id": call_id,
                        "tool_name": name,
                        "input_preview": input_preview,
                    })

                event_id = f"{conv_id}-{step_index}-assistant"
                if event_id not in existing_ids:
                    new_events.append((timestamp, {
                        "timestamp": timestamp,
                        "type": "assistant_message",
                        "event_id": event_id,
                        "source": "antigravity/common_transcript",
                        "role": "assistant",
                        "model": None,
                        # PLANNER_RESPONSE.content is always a string in agy
                        # 1.0.0; the isinstance guard defends against a future
                        # non-string shape by degrading to empty text rather
                        # than crashing the whole converter on a single event.
                        "text": text if isinstance(text, str) else "",
                        "tool_calls": tool_calls,
                        "stop_reason": None,
                        "usage": None,
                        "conversation_id": conv_id,
                        "step_index": step_index,
                    }))
                if tool_calls:
                    last_tool_call_by_conv[conv_id] = tool_calls[-1]

            elif source == "MODEL" and type_ == "CODE_ACTION":
                pending = last_tool_call_by_conv.pop(conv_id, None)
                if pending is None:
                    continue
                event_id = f"{conv_id}-{step_index}-tool_result"
                if event_id in existing_ids:
                    continue
                output = _truncate(raw.get("content", ""), _MAX_OUTPUT_LENGTH)
                # A CODE_ACTION always carries a `status` in agy 1.0.0 (observed
                # value: DONE for a successful action); any other value means the
                # action did not complete cleanly, so is_error is True. The
                # "DONE" default only applies if the field is entirely absent --
                # treat that unobserved shape as success rather than flagging an
                # otherwise-normal result as a scary error in the transcript.
                new_events.append((timestamp, {
                    "timestamp": timestamp,
                    "type": "tool_result",
                    "event_id": event_id,
                    "source": "antigravity/common_transcript",
                    "tool_call_id": pending["tool_call_id"],
                    "tool_name": pending["tool_name"],
                    "output": output,
                    "is_error": raw.get("status", "DONE") != "DONE",
                    "conversation_id": conv_id,
                    "step_index": step_index,
                }))

    if not new_events:
        print("0")
        return

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
        # Forward the heredoc Python's stderr to both the structured log
        # (via log_warn) and the process's stderr -- the latter is what tests
        # and operators read when something has gone wrong with conversion.
        log_warn "convert error: $(cat "$convert_stderr")"
        cat "$convert_stderr" >&2
    fi
    rm -f "$convert_stderr"

    local converted="${result:-0}"
    if [ "$converted" -gt 0 ] 2>/dev/null; then
        log_info "Converted $converted new event(s) -> events/antigravity/common_transcript/events.jsonl"
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
