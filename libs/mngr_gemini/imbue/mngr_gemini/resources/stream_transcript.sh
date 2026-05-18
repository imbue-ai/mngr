#!/usr/bin/env bash
# Robust raw-transcript streaming for gemini agents.
#
# Watches all gemini session JSONL files belonging to this agent (filtered
# by .project_root == $MNGR_AGENT_WORK_DIR) and appends new lines verbatim
# to logs/gemini_transcript/events.jsonl. Designed to handle:
#   - Late-appearing session files (re-checks each poll cycle)
#   - Restarts (per-session offsets are persisted; reconciled via id
#     lookup against the output file)
#   - Multiple session files in a single gemini tmp dir
#
# Per-session line offsets are stored in
# <agent-state-dir>/plugin/gemini/.transcript_offsets/<percent_encoded_path>
# (one file per session file; the filename is the session file's absolute
# path with '%' and '/' percent-encoded, see _offset_key_for) so the
# script can resume efficiently. On startup, stored offsets are verified
# against the output file using id-based lookups -- if the stored offset
# is wrong (e.g. crash between emit and offset save), the script works
# backwards through the session file to find the last line that actually
# made it into the output.
#
# Output is the raw bytes gemini wrote: this script never rewrites or
# reschematises content. The common_transcript.sh converter reads from
# the raw output produced here.
#
# Usage: stream_transcript.sh [--single-pass]
#
# Environment:
#   MNGR_AGENT_STATE_DIR  - agent state directory (contains commands/)
#   MNGR_AGENT_WORK_DIR   - agent's working directory (filters sessions)
#   GEMINI_CONFIG_DIR     - gemini config directory (default ~/.gemini)

set -euo pipefail

AGENT_DATA_DIR="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR must be set}"
WORK_DIR="${MNGR_AGENT_WORK_DIR:?MNGR_AGENT_WORK_DIR must be set}"
GEMINI_DIR="${GEMINI_CONFIG_DIR:-$HOME/.gemini}"
OUTPUT_FILE="$AGENT_DATA_DIR/logs/gemini_transcript/events.jsonl"
OFFSET_DIR="$AGENT_DATA_DIR/plugin/gemini/.transcript_offsets"
POLL_INTERVAL=1

mkdir -p "$(dirname "$OUTPUT_FILE")" "$OFFSET_DIR"
touch "$OUTPUT_FILE"

# Configure and source the shared logging library
_MNGR_LOG_TYPE="stream_transcript"
_MNGR_LOG_SOURCE="logs/stream_transcript"
_MNGR_LOG_FILE="$AGENT_DATA_DIR/events/logs/stream_transcript/events.jsonl"
# shellcheck source=mngr_log.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_log.sh"
# shellcheck source=mngr_transcript_lib.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_transcript_lib.sh"

# Per-session state. Keys are absolute session file paths; values are
# line counts already emitted from that file.
declare -A _OFFSET_BY_PATH=()

# id lookup set, populated by mngr_transcript_build_id_set and cleared
# when reconciliation finishes.
declare -A _MNGR_TRANSCRIPT_ID_SET=()

_line_count() {
    if [ -f "$1" ]; then
        wc -l < "$1"
    else
        echo 0
    fi
}

# Persisted-offset key per session file. The shared
# mngr_transcript_percent_encode_path makes the mapping injective so
# distinct paths produce distinct filenames (a naive '/'-to-'_'
# substitution would alias paths containing underscores).
_load_stored_offset() {
    local key
    key=$(mngr_transcript_percent_encode_path "$1")
    if [ -f "$OFFSET_DIR/$key" ]; then
        cat "$OFFSET_DIR/$key"
    else
        echo 0
    fi
}

_save_offset() {
    local key
    key=$(mngr_transcript_percent_encode_path "$1")
    echo "$2" > "$OFFSET_DIR/$key"
}

# Discover gemini session files belonging to this agent.
# Gemini stores sessions at $GEMINI_DIR/tmp/<dir>/chats/session-*.jsonl with
# the <dir>'s .project_root file pointing at the working directory the CLI
# was launched from. We filter by that marker so multiple gemini agents on
# the same host produce disjoint raw transcripts.
#
# Echoes one absolute path per line.
_find_session_files() {
    local tmp_dir="$GEMINI_DIR/tmp"
    [ -d "$tmp_dir" ] || return 0
    local entry
    for entry in "$tmp_dir"/*/; do
        [ -d "$entry" ] || continue
        local project_root_file="$entry.project_root"
        [ -f "$project_root_file" ] || continue
        local project_root
        project_root=$(< "$project_root_file")
        # Strip any trailing whitespace/newline that may exist.
        project_root="${project_root%$'\n'}"
        [ "$project_root" = "$WORK_DIR" ] || continue
        local chats_dir="${entry}chats"
        [ -d "$chats_dir" ] || continue
        local session_file
        for session_file in "$chats_dir"/session-*.jsonl; do
            [ -f "$session_file" ] || continue
            echo "$session_file"
        done
    done
}

# Append new lines from a session file to the output. The shared
# mngr_transcript_emit_lines_range uses sed with a bounded range to
# avoid a TOCTOU race (see mngr_transcript_lib.sh).
_emit_new_lines() {
    local session_file="$1"
    local offset="${_OFFSET_BY_PATH[$session_file]:-0}"

    local file_lines
    file_lines=$(_line_count "$session_file")

    if [ "$file_lines" -le "$offset" ]; then
        return
    fi

    local start=$((offset + 1))
    mngr_transcript_emit_lines_range "$session_file" "$start" "$file_lines" "$OUTPUT_FILE"

    local new_count=$((file_lines - offset))
    _OFFSET_BY_PATH[$session_file]=$file_lines
    _save_offset "$session_file" "$file_lines"

    log_debug "Emitted $new_count line(s) from $session_file (offset $offset -> $file_lines)"
}

# Load + reconcile a session file's offset, record it in _OFFSET_BY_PATH,
# and persist any change. The caller is responsible for ensuring
# _MNGR_TRANSCRIPT_ID_SET is populated (via mngr_transcript_build_id_set)
# before the call, because mngr_transcript_reconcile_offset depends on it.
#
# $1: absolute session file path
# $2: log prefix used to distinguish startup reconciliations from
#     reconciliations of files that appeared after startup
_record_session_offset() {
    local session_file="$1"
    local log_prefix="$2"

    local stored
    stored=$(_load_stored_offset "$session_file")
    local reconciled
    reconciled=$(mngr_transcript_reconcile_offset "$session_file" "id")
    local effective="$stored"
    if [ "$reconciled" -gt "$stored" ]; then
        effective="$reconciled"
    fi
    _OFFSET_BY_PATH[$session_file]=$effective
    if [ "$effective" != "$stored" ]; then
        log_info "$log_prefix $session_file: $stored -> $effective"
        _save_offset "$session_file" "$effective"
    fi
}

_initialize() {
    mngr_transcript_build_id_set "$OUTPUT_FILE" "id"

    local session_file
    while IFS= read -r session_file; do
        _record_session_offset "$session_file" "Reconciled offset for"
    done < <(_find_session_files)

    log_info "Tracked ${#_OFFSET_BY_PATH[@]} session file(s) at startup"

    # Free the id set -- not needed until next reconciliation
    _MNGR_TRANSCRIPT_ID_SET=()
}

_run_one_cycle() {
    local current_files=()
    local session_file
    while IFS= read -r session_file; do
        current_files+=("$session_file")
    done < <(_find_session_files)

    for session_file in "${current_files[@]}"; do
        if [ -z "${_OFFSET_BY_PATH[$session_file]+exists}" ]; then
            mngr_transcript_build_id_set "$OUTPUT_FILE" "id"
            _record_session_offset "$session_file" "Reconciled late-appearing session"
            _MNGR_TRANSCRIPT_ID_SET=()
        fi
        _emit_new_lines "$session_file"
    done
}

main() {
    local is_single_pass=false
    if [ "${1:-}" = "--single-pass" ]; then
        is_single_pass=true
    fi

    log_info "Stream transcript started"
    log_info "  Gemini dir: $GEMINI_DIR"
    log_info "  Work dir: $WORK_DIR"
    log_info "  Output: $OUTPUT_FILE"
    log_info "  Poll interval: ${POLL_INTERVAL}s"

    _initialize

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
