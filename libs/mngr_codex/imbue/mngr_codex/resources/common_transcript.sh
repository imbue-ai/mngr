#!/usr/bin/env bash
# Common transcript converter daemon for codex agents.
#
# Polls the raw codex rollout stream at logs/codex_transcript/events.jsonl
# (written verbatim by stream_transcript.sh) and, whenever it has grown, shells
# out to common_transcript_convert.py to append new records to
# events/codex/common_transcript/events.jsonl. Each pass runs under the shared
# convert lock, which serializes the poll loop against the turn-end
# --single-pass flush, and the converter's stdout/stderr never reaches this
# watcher's own -- appended counts and converter errors are logged to
# events/logs/common_transcript instead of surfacing in the agent's pane.
#
# What is converted, how the emitted records are shaped, and which native
# rollout items are deliberately dropped is decided entirely by
# common_transcript_convert.py; its module docstring is the single source of
# truth for that contract.
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
