#!/usr/bin/env bash
# Common-transcript converter for grok agents.
#
# Converts the raw transcript at logs/grok_transcript/events.jsonl into
# events/grok/common_transcript/events.jsonl with common_transcript_convert.py
# (installed beside this script), under the per-agent convert lock. Converter
# output and errors go to events/logs/common_transcript, never to the pane.
#
# Usage: common_transcript.sh [--single-pass [--hold-open-steps]]
#   (no arguments)                   poll every POLL_INTERVAL seconds, converting when the input changed;
#                                    a step whose closing record has not arrived is held
#   --single-pass                    convert once, emitting steps still open (final flush)
#   --single-pass --hold-open-steps  convert once, holding steps still open (turn-end flush)
#
# Environment:
#   MNGR_AGENT_STATE_DIR       - agent state directory (contains commands/, events/, logs/)
#   MNGR_CONVERT_LOCK_TIMEOUT  - seconds to wait for the convert lock

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AGENT_DATA_DIR="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR must be set}"
INPUT_FILE="$AGENT_DATA_DIR/logs/grok_transcript/events.jsonl"
OUTPUT_FILE="$AGENT_DATA_DIR/events/grok/common_transcript/events.jsonl"
POLL_INTERVAL=5

# Input size and mtime at the last completed poll pass.
_LAST_CONVERTED_INPUT_SIGNATURE=""

_MNGR_LOG_TYPE="common_transcript"
_MNGR_LOG_SOURCE="logs/common_transcript"
_MNGR_LOG_FILE="$AGENT_DATA_DIR/events/logs/common_transcript/events.jsonl"
# shellcheck source=mngr_log.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_log.sh"
# shellcheck source=mngr_common_transcript_lib.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_common_transcript_lib.sh"

# Print "<size> <mtime>" for the input file (GNU, then BSD stat), or nothing when it is missing.
_input_signature() {
    stat -c '%s %Y' "$INPUT_FILE" 2>/dev/null || stat -f '%z %m' "$INPUT_FILE" 2>/dev/null || true
}

_output_line_count() {
    if [ -f "$OUTPUT_FILE" ]; then
        wc -l < "$OUTPUT_FILE" | tr -d ' '
    else
        echo 0
    fi
}

# Run one conversion pass. $1 is "1" to emit steps still open at the end of the input.
# Returns 1 when the convert lock could not be taken.
convert_new_events() {
    local emit_open_steps="$1"
    if [ ! -f "$INPUT_FILE" ]; then
        return 0
    fi
    if ! mngr_common_transcript_acquire_lock; then
        log_warn "could not acquire convert lock; skipping pass"
        return 1
    fi
    local convert_stderr lines_before converted
    convert_stderr=$(mktemp)
    lines_before=$(_output_line_count)
    _INPUT_FILE="$INPUT_FILE" _OUTPUT_FILE="$OUTPUT_FILE" _MNGR_EMIT_OPEN_STEPS="$emit_open_steps" \
        python3 "$SCRIPT_DIR/common_transcript_convert.py" >/dev/null 2>"$convert_stderr" || true
    converted=$(($(_output_line_count) - lines_before))
    mngr_common_transcript_release_lock
    if [ -s "$convert_stderr" ]; then
        log_warn "convert error: $(cat "$convert_stderr")"
    fi
    rm -f "$convert_stderr"
    if [ "$converted" -gt 0 ]; then
        log_info "Converted $converted new event(s) -> events/grok/common_transcript/events.jsonl"
    fi
    return 0
}

main() {
    local mode="${1:-}"
    local hold_flag="${2:-}"
    mkdir -p "$(dirname "$OUTPUT_FILE")"

    if [ "$mode" = "--single-pass" ]; then
        local emit_open_steps=1
        if [ "$hold_flag" = "--hold-open-steps" ]; then
            emit_open_steps=""
        fi
        convert_new_events "$emit_open_steps" || true
        return 0
    fi

    log_info "Common transcript converter started (input $INPUT_FILE, output $OUTPUT_FILE)"
    while [ -d "$AGENT_DATA_DIR" ]; do
        local current_signature
        current_signature=$(_input_signature)
        if [ "$current_signature" != "$_LAST_CONVERTED_INPUT_SIGNATURE" ]; then
            if convert_new_events ""; then
                _LAST_CONVERTED_INPUT_SIGNATURE="$current_signature"
            fi
        fi
        sleep "$POLL_INTERVAL"
    done
}

main "$@"
