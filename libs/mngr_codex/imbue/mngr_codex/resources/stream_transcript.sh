#!/usr/bin/env bash
# Raw-transcript streaming for codex agents.
#
# codex writes one rollout JSONL per session under
# $CODEX_HOME/sessions/YYYY/MM/DD/rollout-*-<uuid>.jsonl and hands the active
# rollout's absolute path to every hook as `transcript_path`. record_session_pointers.sh
# records that path (at each turn boundary) in
# $MNGR_AGENT_STATE_DIR/codex_transcript_path. This streamer reads that one path
# and tails it, appending every new line verbatim (no reschematising) to
# $MNGR_AGENT_STATE_DIR/logs/codex_transcript/events.jsonl. The downstream
# common_transcript.sh converts that raw output into the common format.
#
# Why re-read the path file each cycle: codex may open a fresh rollout on resume
# (a new session id -> a new rollout path), and the hook re-captures it, so the
# path can change while this streamer runs. Re-reading each cycle keeps the tail
# pointed at the current rollout without restarting.
#
# Per-rollout offsets are stored in
# <agent-state-dir>/plugin/codex/.transcript_offsets/<sanitized-rollout-name>
# (keyed by the rollout file path sanitized to a filename-safe token) so the
# script resumes efficiently after restarts. Two instances of this script can
# run concurrently -- the 1s poll daemon and the turn-end `--single-pass` flush
# (mngr_common_transcript_flush) -- so every emit pass re-reads the persisted
# offset and advances it under the same per-agent lock the converter uses
# (mngr_common_transcript_lib.sh). Serializing the read-offset/append/
# write-offset section means whichever pass runs second sees the offset the
# other just advanced and never re-emits those lines. The rollout lines carry
# no global per-line id we can reconcile against, so we trust the stored
# offset; if a crash occurred between an emit and the matching `_save_offset`,
# restart re-emits at most the duplicate lines. A defensive reset handles a
# rollout that got shorter than the stored offset.
#
# Usage: stream_transcript.sh [--single-pass]
#
# Environment:
#   MNGR_AGENT_STATE_DIR  - agent state directory (contains commands/)

set -euo pipefail

AGENT_DATA_DIR="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR must be set}"
# Path file written by record_session_pointers.sh; kept in sync with
# TRANSCRIPT_PATH_FILENAME in codex_config.py.
TRANSCRIPT_PATH_FILE="$AGENT_DATA_DIR/codex_transcript_path"
OUTPUT_FILE="$AGENT_DATA_DIR/logs/codex_transcript/events.jsonl"
OFFSET_DIR="$AGENT_DATA_DIR/plugin/codex/.transcript_offsets"
POLL_INTERVAL=1

mkdir -p "$(dirname "$OUTPUT_FILE")" "$OFFSET_DIR"
touch "$OUTPUT_FILE"

# Configure and source the shared logging library
_MNGR_LOG_TYPE="stream_transcript"
_MNGR_LOG_SOURCE="logs/stream_transcript"
_MNGR_LOG_FILE="$AGENT_DATA_DIR/events/logs/stream_transcript/events.jsonl"
# shellcheck source=mngr_log.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_log.sh"

# Shared common-transcript primitives: the per-agent lock that serializes this
# streamer's read-offset/append/write-offset section against the concurrent
# `--single-pass` flush (and against the converter's read-modify-write).
# shellcheck source=mngr_common_transcript_lib.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_common_transcript_lib.sh"

_line_count() {
    if [ -f "$1" ]; then
        wc -l < "$1"
    else
        echo 0
    fi
}

_load_stored_offset() {
    if [ -f "$OFFSET_DIR/$1" ]; then
        cat "$OFFSET_DIR/$1"
    else
        echo 0
    fi
}

_save_offset() {
    echo "$2" > "$OFFSET_DIR/$1"
}

# Set _CURRENT_ROLLOUT_PATH to the rollout path the hook recorded, or empty when
# no turn has opened one yet. Assigns to a global via the read builtin instead of
# printing for a command substitution to capture, so discovering the path costs
# no forks at all -- at a one-second poll that subshell was most of an idle cycle.
_CURRENT_ROLLOUT_PATH=""
_read_current_rollout_path() {
    _CURRENT_ROLLOUT_PATH=""
    if [ -f "$TRANSCRIPT_PATH_FILE" ]; then
        read -r _CURRENT_ROLLOUT_PATH < "$TRANSCRIPT_PATH_FILE" || true
    fi
}

# Map a rollout path to a filename-safe offset key. The rollout file name
# (rollout-<date>-<uuid>.jsonl) is already unique and filename-safe, so the
# basename is the natural key; any residual unsafe character is replaced so the
# token is always a valid single path component.
_offset_key_for_path() {
    # Parameter expansion rather than basename piped through tr: both are exact
    # bash equivalents of those tools here, and this runs on every poll cycle.
    local name="${1##*/}"
    printf '%s' "${name//[^A-Za-z0-9._-]/_}"
}

# Append new lines from the current rollout to the output verbatim.
#
# The persisted offset is re-read on every pass, under the shared lock, because
# a concurrent instance of this script (daemon vs `--single-pass` flush) may
# have advanced it since our last pass; trusting a per-process cache here is
# what used to re-emit every line the other instance had already streamed.
#
# Uses a bounded line-range read to avoid a TOCTOU race between the wc and the
# read (any lines appended in between are deferred to the next poll cycle).
_emit_new_lines() {
    local rollout_path="$1"
    if [ ! -f "$rollout_path" ]; then
        return
    fi
    local key
    key=$(_offset_key_for_path "$rollout_path")

    if ! mngr_common_transcript_acquire_lock; then
        log_warn "Could not acquire transcript lock for rollout $key; deferring emit"
        return
    fi

    local offset
    offset=$(_load_stored_offset "$key")

    local file_lines
    file_lines=$(_line_count "$rollout_path")

    # Defensive reset: if the on-disk rollout got shorter than the stored offset
    # (e.g. codex rewrote/replaced the file), start from 0 rather than silently
    # skipping the rest.
    if [ "$file_lines" -lt "$offset" ]; then
        log_warn "Rollout $key: stored offset $offset > file lines $file_lines; resetting to 0"
        offset=0
        _save_offset "$key" 0
    fi

    if [ "$file_lines" -le "$offset" ]; then
        mngr_common_transcript_release_lock
        return
    fi

    local start=$((offset + 1))
    local end="$file_lines"
    local new_count=$((file_lines - offset))

    # Append the new lines verbatim. The downstream converter parses them; this
    # streamer never rewrites content.
    sed -n "${start},${end}p" "$rollout_path" >> "$OUTPUT_FILE"

    _save_offset "$key" "$file_lines"

    mngr_common_transcript_release_lock

    log_debug "Emitted $new_count line(s) from rollout $key (offset $offset -> $file_lines)"
}

_run_one_cycle() {
    _read_current_rollout_path
    local rollout_path="$_CURRENT_ROLLOUT_PATH"
    # No rollout path recorded yet (no turn has opened) -> nothing to stream.
    if [ -z "$rollout_path" ]; then
        return
    fi
    _emit_new_lines "$rollout_path"
}

main() {
    local is_single_pass=false
    if [ "${1:-}" = "--single-pass" ]; then
        is_single_pass=true
    fi

    log_info "Stream transcript started"
    log_info "  Transcript path file: $TRANSCRIPT_PATH_FILE"
    log_info "  Output: $OUTPUT_FILE"
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
