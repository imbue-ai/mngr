#!/bin/bash
# Robust transcript streaming for Claude agents.
#
# Watches ALL Claude session JSONL files and appends new lines to
# logs/claude_transcript/events.jsonl. Designed to handle:
#   - Any session file being written to at any time (not just the "current" one)
#   - Restarts (reconciles per-session offsets against the output file)
#   - Late-appearing session files (re-checks each poll cycle, no timeouts)
#   - Sessions added out of order or with gaps
#
# Per-session line offsets are stored in
# <agent-state-dir>/plugin/claude/.transcript_offsets/<session_id> so the
# script can resume efficiently. On startup, stored offsets are verified
# against the output file using UUID-based lookups -- if the stored offset
# is wrong (e.g. crash between emit and offset save), the script works
# backwards through the session file to find the last line that actually
# made it into the output.
#
# Usage: stream_transcript.sh
#
# Requires environment variables:
#   MNG_AGENT_STATE_DIR  - the agent's state directory
#   MNG_HOST_DIR         - the host data directory (contains commands/)

set -euo pipefail

SESSION_HISTORY="${MNG_AGENT_STATE_DIR:?MNG_AGENT_STATE_DIR must be set}/claude_session_id_history"
OUTPUT_FILE="$MNG_AGENT_STATE_DIR/logs/claude_transcript/events.jsonl"
OFFSET_DIR="$MNG_AGENT_STATE_DIR/plugin/claude/.transcript_offsets"
POLL_INTERVAL=1

mkdir -p "$(dirname "$OUTPUT_FILE")" "$OFFSET_DIR"
touch "$OUTPUT_FILE"

# Configure and source the shared logging library
_MNG_LOG_TYPE="stream_transcript"
_MNG_LOG_SOURCE="logs/stream_transcript"
_MNG_LOG_FILE="$MNG_HOST_DIR/events/logs/stream_transcript/events.jsonl"
# shellcheck source=mng_log.sh
source "$MNG_HOST_DIR/commands/mng_log.sh"

# -- Per-session state (bash 4+ associative arrays) --
# Note: explicit =() is required for set -u compatibility (empty associative
# arrays are "unbound" under set -u without it).
declare -A _FILE_BY_SID=()    # session_id -> resolved file path ("" if not yet found)
declare -A _OFFSET_BY_SID=()  # session_id -> lines already emitted from this session
_KNOWN_HISTORY_LINES=0        # lines of the history file already processed

# UUID lookup set, built once at startup for offset reconciliation
declare -A _OUTPUT_UUIDS=()

# -- Helpers --

# Find the JSONL file for a session ID.
# Claude stores session files at ~/.claude/projects/<hash>/<session_id>.jsonl
_find_session_jsonl() {
    find ~/.claude/projects/ -name "${1}.jsonl" 2>/dev/null | head -1
}

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

# Try to resolve the file path for a session (caches result once found).
_try_resolve_file() {
    local sid="$1"
    if [ -n "${_FILE_BY_SID[$sid]:-}" ]; then
        return 0
    fi
    local path
    path=$(_find_session_jsonl "$sid")
    if [ -n "$path" ] && [ -f "$path" ]; then
        _FILE_BY_SID[$sid]="$path"
        log_debug "Resolved session $sid -> $path"
        return 0
    fi
    return 1
}

# Extract the uuid field from a single JSONL line (no jq, for speed).
_extract_uuid() {
    grep -o '"uuid": *"[^"]*"' <<< "$1" 2>/dev/null | head -1 | cut -d'"' -f4
}

# -- Reconciliation (restart recovery) --

# Build UUID lookup set from all lines in the output file.
# Called once at startup and again if a late-appearing file needs reconciliation.
_build_output_uuid_set() {
    _OUTPUT_UUIDS=()
    if [ ! -s "$OUTPUT_FILE" ]; then
        return
    fi
    while IFS= read -r uuid; do
        [ -n "$uuid" ] && _OUTPUT_UUIDS["$uuid"]=1
    done < <(grep -o '"uuid": *"[^"]*"' "$OUTPUT_FILE" | cut -d'"' -f4)
    log_debug "Built UUID set with ${#_OUTPUT_UUIDS[@]} entries"
}

# Find the true offset for a session file by working backwards from the end
# to find the last line whose UUID is already in the output file. This
# handles crash recovery correctly: if we crashed after emitting lines
# N+1..M but before saving the offset, the backwards scan finds line M
# (the actual last emitted line) rather than the stale stored offset N.
_reconcile_offset() {
    local sid="$1"
    local session_file="$2"

    local file_lines
    file_lines=$(_line_count "$session_file")

    # If output is empty, everything needs to be emitted
    if [ ${#_OUTPUT_UUIDS[@]} -eq 0 ]; then
        echo 0
        return
    fi

    # If session file is empty, nothing to reconcile
    if [ "$file_lines" -eq 0 ]; then
        echo 0
        return
    fi

    # Work backwards through the file to find the last emitted line
    log_debug "Reconciling offset for $sid (file_lines=$file_lines)"
    local reverse_idx=0
    while IFS= read -r line; do
        reverse_idx=$((reverse_idx + 1))
        local uuid
        uuid=$(_extract_uuid "$line")
        if [ -n "$uuid" ] && [ "${_OUTPUT_UUIDS[$uuid]+exists}" ]; then
            local found=$((file_lines - reverse_idx + 1))
            log_debug "Found last emitted line at $found for $sid"
            echo "$found"
            return
        fi
    done < <(tac "$session_file")

    echo 0
}

# -- Session processing --

# Check a session file for new lines and append them to the output.
# Uses sed with a bounded range to avoid a TOCTOU race: wc -l captures the
# line count at time T1, and sed reads exactly lines offset+1..file_lines.
# If Claude appends more lines between T1 and the sed read, those extra lines
# are NOT emitted (they'll be picked up on the next poll cycle), and the
# saved offset accurately reflects what was actually emitted.
_emit_new_lines() {
    local sid="$1"
    local session_file="${_FILE_BY_SID[$sid]}"
    local offset="${_OFFSET_BY_SID[$sid]}"

    local file_lines
    file_lines=$(_line_count "$session_file")

    if [ "$file_lines" -le "$offset" ]; then
        return
    fi

    local start=$((offset + 1))
    sed -n "${start},${file_lines}p" "$session_file" >> "$OUTPUT_FILE"

    local new_count=$((file_lines - offset))
    _OFFSET_BY_SID[$sid]=$file_lines
    _save_offset "$sid" "$file_lines"

    log_debug "Emitted $new_count line(s) from session $sid (offset $offset -> $file_lines)"
}

# Check the history file for new session IDs.
_check_for_new_sessions() {
    [ -f "$SESSION_HISTORY" ] || return 0

    local current_lines
    current_lines=$(_line_count "$SESSION_HISTORY")
    [ "$current_lines" -le "$_KNOWN_HISTORY_LINES" ] && return 0

    local start=$((_KNOWN_HISTORY_LINES + 1))
    while read -r sid _rest; do
        if [ -n "$sid" ] && [ -z "${_FILE_BY_SID[$sid]+exists}" ]; then
            _FILE_BY_SID[$sid]=""
            _OFFSET_BY_SID[$sid]=0
            log_info "Discovered new session: $sid"
        fi
    done < <(tail -n "+${start}" "$SESSION_HISTORY")

    _KNOWN_HISTORY_LINES=$current_lines
}

# -- Initialization --

_initialize() {
    # Load all known sessions from history
    if [ -f "$SESSION_HISTORY" ]; then
        while read -r sid _rest; do
            if [ -n "$sid" ]; then
                _FILE_BY_SID[$sid]=""
                _OFFSET_BY_SID[$sid]=$(_load_stored_offset "$sid")
            fi
        done < "$SESSION_HISTORY"
        _KNOWN_HISTORY_LINES=$(_line_count "$SESSION_HISTORY")
    fi

    log_info "Loaded ${#_FILE_BY_SID[@]} session(s) from history"

    # Build UUID set from the output file for reconciliation
    _build_output_uuid_set

    # Resolve files and reconcile offsets for all known sessions
    for sid in "${!_FILE_BY_SID[@]}"; do
        if _try_resolve_file "$sid"; then
            local stored="${_OFFSET_BY_SID[$sid]}"
            local reconciled
            reconciled=$(_reconcile_offset "$sid" "${_FILE_BY_SID[$sid]}")
            _OFFSET_BY_SID[$sid]=$reconciled
            if [ "$reconciled" != "$stored" ]; then
                log_info "Reconciled offset for $sid: $stored -> $reconciled"
                _save_offset "$sid" "$reconciled"
            fi
        fi
    done

    # Free the UUID set -- not needed until next reconciliation
    _OUTPUT_UUIDS=()
}

# -- Poll cycle (shared by main loop and single-pass mode) --

_run_one_cycle() {
    _check_for_new_sessions

    for sid in "${!_FILE_BY_SID[@]}"; do
        # Try to resolve file if not yet found (re-checked every cycle)
        if [ -z "${_FILE_BY_SID[$sid]}" ]; then
            if ! _try_resolve_file "$sid"; then
                continue
            fi
            # File just appeared -- reconcile against the output file to
            # find the true offset (handles both fresh starts and restarts)
            _build_output_uuid_set
            stored="${_OFFSET_BY_SID[$sid]}"
            reconciled=$(_reconcile_offset "$sid" "${_FILE_BY_SID[$sid]}")
            _OFFSET_BY_SID[$sid]=$reconciled
            if [ "$reconciled" != "$stored" ]; then
                log_info "Reconciled late-appearing session $sid: $stored -> $reconciled"
                _save_offset "$sid" "$reconciled"
            fi
            _OUTPUT_UUIDS=()
        fi

        _emit_new_lines "$sid"
    done
}

# -- Main --

main() {
    local is_single_pass=false
    if [ "${1:-}" = "--single-pass" ]; then
        is_single_pass=true
    fi

    log_info "Stream transcript started"
    log_info "  Session history: $SESSION_HISTORY"
    log_info "  Output: $OUTPUT_FILE"
    log_info "  Poll interval: ${POLL_INTERVAL}s"

    _initialize

    # Emit any backlog in history-file order (for rough ordering on startup)
    if [ -f "$SESSION_HISTORY" ]; then
        while read -r sid _rest; do
            if [ -n "$sid" ] && [ -n "${_FILE_BY_SID[$sid]:-}" ]; then
                _emit_new_lines "$sid"
            fi
        done < "$SESSION_HISTORY"
    fi

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
