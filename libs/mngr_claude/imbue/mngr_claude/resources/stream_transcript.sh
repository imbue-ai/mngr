#!/usr/bin/env bash
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
#   MNGR_AGENT_STATE_DIR  - the agent's state directory (contains commands/)

set -euo pipefail

SESSION_HISTORY="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR must be set}/claude_session_id_history"
OUTPUT_FILE="$MNGR_AGENT_STATE_DIR/logs/claude_transcript/events.jsonl"
OFFSET_DIR="$MNGR_AGENT_STATE_DIR/plugin/claude/.transcript_offsets"
POLL_INTERVAL=1

mkdir -p "$(dirname "$OUTPUT_FILE")" "$OFFSET_DIR"
touch "$OUTPUT_FILE"

# Configure and source the shared logging library
_MNGR_LOG_TYPE="stream_transcript"
_MNGR_LOG_SOURCE="logs/stream_transcript"
_MNGR_LOG_FILE="$MNGR_AGENT_STATE_DIR/events/logs/stream_transcript/events.jsonl"
# shellcheck source=mngr_log.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_log.sh"
# shellcheck source=mngr_transcript_lib.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_transcript_lib.sh"

# -- Per-session state (bash 4+ associative arrays) --
# Note: explicit =() is required for set -u compatibility (empty associative
# arrays are "unbound" under set -u without it).
declare -A _FILE_BY_SID=()    # session_id -> resolved file path ("" if not yet found)
declare -A _OFFSET_BY_SID=()  # session_id -> lines already emitted from this session
_KNOWN_HISTORY_LINES=0        # lines of the history file already processed

# UUID lookup set, populated by mngr_transcript_build_id_set during
# reconciliation and cleared once reconciliation finishes.
declare -A _MNGR_TRANSCRIPT_ID_SET=()

# -- Helpers --

# Find the JSONL file for a session ID.
# Claude stores session files at $CLAUDE_CONFIG_DIR/projects/<hash>/<session_id>.jsonl
# Falls back to ~/.claude/projects/ when CLAUDE_CONFIG_DIR is not set.
_find_session_jsonl() {
    find "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/" -name "${1}.jsonl" 2>/dev/null | head -1
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

# -- Reconciliation (restart recovery) --
#
# Field extraction, id-set construction, and reverse-scan reconciliation
# come from mngr_transcript_lib.sh. The shared helpers operate on the
# global _MNGR_TRANSCRIPT_ID_SET, populated by mngr_transcript_build_id_set.

# -- Session processing --

# Check a session file for new lines and append them to the output.
# The shared mngr_transcript_emit_lines_range uses sed with a bounded range
# to avoid a TOCTOU race: wc -l captures the line count at time T1, and
# sed reads exactly lines offset+1..file_lines. If Claude appends more
# lines between T1 and the sed read, those extra lines are NOT emitted
# (they'll be picked up on the next poll cycle), and the saved offset
# accurately reflects what was actually emitted.
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
    mngr_transcript_emit_lines_range "$session_file" "$start" "$file_lines" "$OUTPUT_FILE"

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
    mngr_transcript_build_id_set "$OUTPUT_FILE" "uuid"

    # Resolve files and reconcile offsets for all known sessions
    for sid in "${!_FILE_BY_SID[@]}"; do
        if _try_resolve_file "$sid"; then
            local stored="${_OFFSET_BY_SID[$sid]}"
            local reconciled
            reconciled=$(mngr_transcript_reconcile_offset "${_FILE_BY_SID[$sid]}" "uuid")
            _OFFSET_BY_SID[$sid]=$reconciled
            if [ "$reconciled" != "$stored" ]; then
                log_info "Reconciled offset for $sid: $stored -> $reconciled"
                _save_offset "$sid" "$reconciled"
            fi
        fi
    done

    # Free the UUID set -- not needed until next reconciliation
    _MNGR_TRANSCRIPT_ID_SET=()
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
            mngr_transcript_build_id_set "$OUTPUT_FILE" "uuid"
            stored="${_OFFSET_BY_SID[$sid]}"
            reconciled=$(mngr_transcript_reconcile_offset "${_FILE_BY_SID[$sid]}" "uuid")
            _OFFSET_BY_SID[$sid]=$reconciled
            if [ "$reconciled" != "$stored" ]; then
                log_info "Reconciled late-appearing session $sid: $stored -> $reconciled"
                _save_offset "$sid" "$reconciled"
            fi
            _MNGR_TRANSCRIPT_ID_SET=()
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
