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

# Directory this script was installed into; the converter module is installed
# alongside it (in the agent's commands/ dir in production, in resources/ under
# test), so resolve it relative to ourselves.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AGENT_DATA_DIR="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR must be set}"
INPUT_FILE="$AGENT_DATA_DIR/logs/codex_transcript/events.jsonl"
OUTPUT_FILE="$AGENT_DATA_DIR/events/codex/common_transcript/events.jsonl"
POLL_INTERVAL=5

# Size and mtime of the input file as of the last completed conversion pass. A
# pass re-reads the whole input file and the whole output file, so running one
# when nothing was appended costs a python startup plus a full parse of both for
# no result. Kept in memory only: after a restart it is empty, so the first pass
# always runs and reconciles against the output exactly as before.
_LAST_CONVERTED_INPUT_SIGNATURE=""

_MNGR_LOG_TYPE="common_transcript"
_MNGR_LOG_SOURCE="logs/common_transcript"
_MNGR_LOG_FILE="$AGENT_DATA_DIR/events/logs/common_transcript/events.jsonl"
# shellcheck source=mngr_log.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_log.sh"

# Shared common-transcript primitives: the convert lock that serializes this
# converter's read-modify-write against the turn-end --single-pass flush (see
# the library header for why duplicates would result without it).
# shellcheck source=mngr_common_transcript_lib.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_common_transcript_lib.sh"

# Print "<size> <mtime>" for the input file, or nothing when it does not exist.
# GNU and BSD stat spell these differently and agents run on Linux hosts as well
# as on a developer's macOS machine under the local provider, so try both. Always
# succeeds so a missing file is an empty signature rather than a failed pass.
_input_signature() {
    stat -c '%s %Y' "$INPUT_FILE" 2>/dev/null || stat -f '%z %m' "$INPUT_FILE" 2>/dev/null || true
}

convert_new_events() {
    if [ ! -f "$INPUT_FILE" ]; then
        log_debug "Input file not found: $INPUT_FILE"
        return
    fi

    # Report the skip to the caller so the poll loop retries on the next cycle
    # instead of recording this input as already converted.
    if ! mngr_common_transcript_acquire_lock; then
        log_warn "could not acquire convert lock; skipping pass"
        return 1
    fi

    local convert_stderr
    convert_stderr=$(mktemp)
    # The converter prints the count of appended events to stdout; capture it
    # here so it never reaches this watcher's stdout (which would surface in the
    # agent's pane). Genuine errors go to stderr.
    local result
    result=$(_INPUT_FILE="$INPUT_FILE" _OUTPUT_FILE="$OUTPUT_FILE" \
        python3 "$SCRIPT_DIR/common_transcript_convert.py" 2>"$convert_stderr" || true)

    # The read-modify-write is done; drop the lock before the (lock-free)
    # logging below so a concurrent pass can proceed immediately.
    mngr_common_transcript_release_lock

    if [ -s "$convert_stderr" ]; then
        # A genuine converter error is logged (to events/logs/common_transcript)
        # but never echoed to this watcher's stdout/stderr -- that would surface
        # in the agent's pane.
        log_warn "convert error: $(cat "$convert_stderr")"
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
        # A pass skipped for a held lock is not a script failure -- the caller
        # only needs to know the flush was attempted.
        convert_new_events || true
        return
    fi

    while true; do
        # Only pay for a pass when the input actually changed. The signature
        # advances only after a pass completes, so a pass skipped for a held
        # lock is retried on the next cycle rather than being lost.
        local current_signature
        current_signature=$(_input_signature)
        if [ "$current_signature" != "$_LAST_CONVERTED_INPUT_SIGNATURE" ]; then
            if convert_new_events; then
                _LAST_CONVERTED_INPUT_SIGNATURE="$current_signature"
            fi
        fi
        sleep "$POLL_INTERVAL"
    done
}

main "${1:-}"
