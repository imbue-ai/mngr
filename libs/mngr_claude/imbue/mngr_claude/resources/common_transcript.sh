#!/usr/bin/env bash
# Transcript watcher for claude agents.
#
# Watches the raw Claude transcript at
# logs/claude_transcript/events.jsonl (produced by stream_transcript.sh)
# and converts semantically important events (user input, assistant output,
# tool calls, tool results) into a common, agent-agnostic format at
# events/claude/common_transcript/events.jsonl.
#
# Noise like progress events, file-history snapshots, and system
# bookkeeping is dropped.
#
# Each output line is a self-describing JSON object: a `header`, `step` or
# `observation` record in the ATIF-shaped stream format (framing fields
# type/event_id/emitter plus ATIF fields; see
# specs/atif-transcript-alignment/spec.md).
#
# The converter uses an ID-based dedup strategy: each output event_id
# is derived from the source event's uuid, so re-processing the same
# input never produces duplicate output.
#
# Usage: common_transcript.sh
#
# Environment:
#   MNGR_AGENT_STATE_DIR  - agent state directory (contains events/, logs/)

set -euo pipefail

# Directory this script was installed into; the converter module is installed
# alongside it (in the agent's commands/ dir in production, in resources/ under
# test), so resolve it relative to ourselves.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AGENT_DATA_DIR="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR must be set}"
INPUT_FILE="$AGENT_DATA_DIR/logs/claude_transcript/events.jsonl"
OUTPUT_FILE="$AGENT_DATA_DIR/events/claude/common_transcript/events.jsonl"
POLL_INTERVAL=5
# How long a turn-end flush waits before its one retry for the convert lock (see
# convert_new_events).
LOCK_RETRY_DELAY=2

# Size and mtime of the input file as of the last completed conversion pass. A
# pass re-reads the whole input file and the whole output file, so running one
# when nothing was appended costs a python startup plus a full parse of both for
# no result. Kept in memory only: after a restart it is empty, so the first pass
# always runs and reconciles against the output exactly as before.
_LAST_CONVERTED_INPUT_SIGNATURE=""

# Configure and source the shared logging library
_MNGR_LOG_TYPE="common_transcript"
_MNGR_LOG_SOURCE="logs/common_transcript"
_MNGR_LOG_FILE="$AGENT_DATA_DIR/events/logs/common_transcript/events.jsonl"
# shellcheck source=mngr_log.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_log.sh"

# Shared common-transcript primitives: the convert lock that serializes this
# converter's read-modify-write against any concurrent --single-pass flush (see
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

# Convert new Claude transcript events to the common format.
#
# Reads the full input file and the set of event_ids already in the output
# file, then appends any new events whose IDs are not yet present. The
# ID-based dedup ensures correctness even if the input file is replayed.
#
# $1: "true" when this pass reads an input that is known to be complete, which
#     lets the converter emit the last (otherwise still-growing) assistant
#     inference. See the comment on _MNGR_EMIT_TRAILING_ASSISTANT_GROUP below.
convert_new_events() {
    local is_input_complete="$1"
    if [ ! -f "$INPUT_FILE" ]; then
        log_debug "Input file not found: $INPUT_FILE"
        return
    fi

    if ! mngr_common_transcript_acquire_lock; then
        # A poll pass reports the skip to the caller, which retries on the next
        # cycle instead of recording this input as already converted. A turn-end
        # flush has no next cycle -- it is the only pass allowed to emit the turn's
        # final inference -- so it waits and tries once more before giving up.
        if [ "$is_input_complete" != true ]; then
            log_warn "could not acquire convert lock; skipping pass"
            return 1
        fi
        sleep "$LOCK_RETRY_DELAY"
        if ! mngr_common_transcript_acquire_lock; then
            log_warn "could not acquire convert lock (after one retry); skipping flush"
            return 1
        fi
    fi

    local convert_stderr
    convert_stderr=$(mktemp)
    # Claude fans one API response out over several JSONL lines that arrive over
    # seconds, and the converter emits one record per response and dedups by
    # event_id -- so a response emitted before its last line landed would stay
    # truncated forever. The converter therefore holds back the file's final
    # assistant response unless this variable says the input is complete. Only the
    # --single-pass path (the turn-end flush, where the turn is over) may say so;
    # the poll loop below runs mid-turn and must not.
    local emit_trailing_group=""
    if [ "$is_input_complete" = true ]; then
        emit_trailing_group=1
    fi
    # The converter prints the count of appended events to stdout; capture it
    # here so it never reaches this watcher's stdout (which would surface in the
    # agent's pane). Genuine errors go to stderr.
    local result
    result=$(_INPUT_FILE="$INPUT_FILE" \
        _OUTPUT_FILE="$OUTPUT_FILE" \
        _MNGR_EMIT_TRAILING_ASSISTANT_GROUP="$emit_trailing_group" \
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
        log_info "Converted $converted new event(s) -> events/claude/common_transcript/events.jsonl"
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
        convert_new_events true || true
        return
    fi

    while true; do
        # Only pay for a pass when the input actually changed. The signature
        # advances only after a pass completes, so a pass skipped for a held
        # lock is retried on the next cycle rather than being lost.
        local current_signature
        current_signature=$(_input_signature)
        if [ "$current_signature" != "$_LAST_CONVERTED_INPUT_SIGNATURE" ]; then
            if convert_new_events false; then
                _LAST_CONVERTED_INPUT_SIGNATURE="$current_signature"
            fi
        fi
        sleep "$POLL_INTERVAL"
    done
}

main "${1:-}"
