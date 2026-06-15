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
#   USER_EXPLICIT/USER_INPUT       -> user_message  (the clean typed text
#                                       agy records in CortexStepUserInput.query)
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

# Directory this script was installed into; the converter module is installed
# alongside it (in the agent's commands/ dir in production, in resources/ under
# test), so resolve it relative to ourselves.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
    local before_count
    before_count=$(wc -l < "$OUTPUT_FILE" 2>/dev/null || echo 0)
    _INPUT_FILE="$INPUT_FILE" _OUTPUT_FILE="$OUTPUT_FILE" \
        python3 "$SCRIPT_DIR/common_transcript_convert.py" 2>"$convert_stderr" || true

    if [ -s "$convert_stderr" ]; then
        # Forward the converter's stderr to both the structured log (via
        # log_warn) and the process's stderr -- the latter is what tests and
        # operators read when something has gone wrong with conversion.
        log_warn "convert error: $(cat "$convert_stderr")"
        cat "$convert_stderr" >&2
    fi
    rm -f "$convert_stderr"

    local after_count
    after_count=$(wc -l < "$OUTPUT_FILE" 2>/dev/null || echo 0)
    local converted=$((after_count - before_count))
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
