#!/usr/bin/env bash
# Raw-transcript streaming for antigravity agents.
#
# Since agy 1.0.4 (2026-06-01) the interactive conversation store is a protobuf
# SQLite .db per conversation under
# $ANTIGRAVITY_APP_DATA_DIR/conversations/<conv_id>.db (earlier agy wrote a
# per-conversation JSONL transcript that this script used to tail; see
# libs/mngr_antigravity/regenerating_protobuf_schema.md for the migration and the recovered schema).
#
# This script is now a thin supervisor around decode_agy_transcript.py, which
# reads new `steps` rows from each of this agent's conversation .db files and
# appends one JSON record per step to
# $MNGR_AGENT_STATE_DIR/logs/antigravity_transcript/events.jsonl -- in the same
# shape the old JSONL had, so common_transcript.sh converts them unchanged. The
# Python decoder owns conversation discovery (from the capture-hook ids file),
# per-conversation step offsets, and the protobuf decode; this script only
# guards python3 and loops it.
#
# Usage: stream_transcript.sh [--single-pass]
#
# Environment:
#   MNGR_AGENT_STATE_DIR        - agent state directory (contains commands/)
#   ANTIGRAVITY_APP_DATA_DIR    - agy app-data dir (default ~/.gemini/antigravity-cli)

set -euo pipefail

AGENT_DATA_DIR="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR must be set}"
APP_DATA_DIR="${ANTIGRAVITY_APP_DATA_DIR:-$HOME/.gemini/antigravity-cli}"
DECODER="$AGENT_DATA_DIR/commands/decode_agy_transcript.py"
POLL_INTERVAL=1

# Configure and source the shared logging library
_MNGR_LOG_TYPE="stream_transcript"
_MNGR_LOG_SOURCE="logs/stream_transcript"
_MNGR_LOG_FILE="$AGENT_DATA_DIR/events/logs/stream_transcript/events.jsonl"
# shellcheck source=mngr_log.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_log.sh"

# Reading agy's SQLite conversation store requires python3 on the agent host
# (the decoder is a dependency-free stdlib script). Fail loudly rather than
# silently capturing no transcript if it is missing.
if ! command -v python3 >/dev/null 2>&1; then
    log_error "python3 is required to decode agy's SQLite conversation store but was not found on PATH; antigravity transcript capture is disabled"
    exit 1
fi

# Run one decode pass, forwarding the decoder's stderr to the structured log.
_run_one_cycle() {
    local stderr_file
    stderr_file=$(mktemp)
    if ! python3 "$DECODER" --state-dir "$AGENT_DATA_DIR" --app-data-dir "$APP_DATA_DIR" 2>"$stderr_file"; then
        log_warn "decode pass failed: $(cat "$stderr_file")"
    elif [ -s "$stderr_file" ]; then
        log_warn "decode pass warning: $(cat "$stderr_file")"
    fi
    rm -f "$stderr_file"
}

main() {
    local is_single_pass=false
    if [ "${1:-}" = "--single-pass" ]; then
        is_single_pass=true
    fi

    log_info "Stream transcript started"
    log_info "  App data dir: $APP_DATA_DIR"
    log_info "  Decoder: $DECODER"
    log_info "  Poll interval: ${POLL_INTERVAL}s"

    if [ "$is_single_pass" = true ]; then
        _run_one_cycle
        return
    fi

    log_info "Entering main loop"
    while true; do
        _run_one_cycle
        sleep "$POLL_INTERVAL"
    done
}

main "${1:-}"
