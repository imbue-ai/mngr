#!/bin/bash
# Efficient transcript streaming for Claude agents using tail -f.
#
# Instead of periodically re-exporting the entire transcript every 15 seconds,
# this script uses tail -f on two levels:
#   1. Watches claude_session_id_history (with stdbuf -oL) to detect new sessions
#   2. Watches the current session's JSONL file (with stdbuf -oL) and appends
#      new lines to logs/claude_transcript/events.jsonl
#
# On startup, any existing sessions are caught up by dumping their content
# (skipping lines already present in the output file).
#
# Usage: stream_transcript.sh
#
# Requires environment variables:
#   MNG_AGENT_STATE_DIR  - the agent's state directory
#   MNG_HOST_DIR         - the host data directory (contains commands/)

set -euo pipefail

SESSION_HISTORY="${MNG_AGENT_STATE_DIR:?MNG_AGENT_STATE_DIR must be set}/claude_session_id_history"
OUTPUT_FILE="$MNG_AGENT_STATE_DIR/logs/claude_transcript/events.jsonl"

mkdir -p "$(dirname "$OUTPUT_FILE")"
touch "$OUTPUT_FILE"

# Configure and source the shared logging library
_MNG_LOG_TYPE="stream_transcript"
_MNG_LOG_SOURCE="stream_transcript"
_MNG_LOG_FILE="$MNG_HOST_DIR/logs/stream_transcript/events.jsonl"
# shellcheck source=mng_log.sh
source "$MNG_HOST_DIR/commands/mng_log.sh"

# PID of the current session tail process
_SESSION_TAIL_PID=""

_cleanup() {
    if [ -n "$_SESSION_TAIL_PID" ] && kill -0 "$_SESSION_TAIL_PID" 2>/dev/null; then
        kill "$_SESSION_TAIL_PID" 2>/dev/null
        wait "$_SESSION_TAIL_PID" 2>/dev/null || true
    fi
    # Kill any other background children (e.g. the history tail)
    local child_pids
    child_pids=$(jobs -p 2>/dev/null || true)
    if [ -n "$child_pids" ]; then
        # shellcheck disable=SC2086
        kill $child_pids 2>/dev/null || true
        wait 2>/dev/null || true
    fi
}
trap _cleanup EXIT

# Find the JSONL file for a session ID.
# Claude stores session files at ~/.claude/projects/<hash>/<session_id>.jsonl
_find_session_jsonl() {
    find ~/.claude/projects/ -name "${1}.jsonl" 2>/dev/null | head -1
}

# Count lines in a file (0 if file does not exist).
_line_count() {
    if [ -f "$1" ]; then
        wc -l < "$1"
    else
        echo 0
    fi
}

# Output remaining lines from a completed session file, given how many
# lines from this session are already in the output.
_dump_remaining_lines() {
    local session_file="$1"
    local already_output="$2"

    local total_lines
    total_lines=$(_line_count "$session_file")

    if [ "$already_output" -lt "$total_lines" ]; then
        local skip_lines=$((already_output + 1))
        tail -n "+${skip_lines}" "$session_file" >> "$OUTPUT_FILE"
        log_debug "Dumped $((total_lines - already_output)) lines from $session_file"
    fi
}

# Kill the current session tail if running.
_kill_session_tail() {
    if [ -n "$_SESSION_TAIL_PID" ] && kill -0 "$_SESSION_TAIL_PID" 2>/dev/null; then
        kill "$_SESSION_TAIL_PID" 2>/dev/null
        wait "$_SESSION_TAIL_PID" 2>/dev/null || true
        _SESSION_TAIL_PID=""
    fi
}

# Start tailing a session's JSONL file from the given line offset,
# appending new lines to the output file.
_start_session_tail() {
    local session_id="$1"
    local already_output="$2"

    _kill_session_tail

    # Wait for the session file to appear (Claude may not have written it yet)
    local session_file=""
    local wait_count=0
    while [ -z "$session_file" ] && [ "$wait_count" -lt 60 ]; do
        session_file=$(_find_session_jsonl "$session_id")
        if [ -z "$session_file" ]; then
            sleep 1
            wait_count=$((wait_count + 1))
        fi
    done

    if [ -z "$session_file" ]; then
        log_warn "Session file for $session_id not found after 60s"
        return 1
    fi

    # Start tailing from the correct position
    local start_line=$((already_output + 1))
    stdbuf -oL tail -n "+${start_line}" -f "$session_file" >> "$OUTPUT_FILE" &
    _SESSION_TAIL_PID=$!

    log_info "Tailing session $session_id from line $start_line (file=$session_file, PID=$_SESSION_TAIL_PID)"
    return 0
}

# Catch up on existing sessions and start tailing the latest one.
#
# Walks through all session IDs in the history file, compares against
# lines already in the output file, dumps any missing content from
# completed sessions, and starts tail -f on the last (current) session.
_catch_up_and_tail() {
    local -a session_ids=()

    if [ -f "$SESSION_HISTORY" ]; then
        while read -r sid _rest; do
            if [ -n "$sid" ]; then
                session_ids+=("$sid")
            fi
        done < "$SESSION_HISTORY"
    fi

    if [ ${#session_ids[@]} -eq 0 ]; then
        log_debug "No sessions in history yet"
        return 1
    fi

    # Count lines already in the output file
    local output_lines
    output_lines=$(_line_count "$OUTPUT_FILE")
    local remaining=$output_lines

    local last_idx=$(( ${#session_ids[@]} - 1 ))

    for i in "${!session_ids[@]}"; do
        local sid="${session_ids[$i]}"
        local session_file
        session_file=$(_find_session_jsonl "$sid")

        if [ -z "$session_file" ] || [ ! -f "$session_file" ]; then
            log_debug "Session file for $sid not found, skipping"
            continue
        fi

        local session_lines
        session_lines=$(_line_count "$session_file")

        if [ "$i" -eq "$last_idx" ]; then
            # This is the current session -- start tailing from where we left off
            local already_output=0
            if [ "$remaining" -ge "$session_lines" ]; then
                already_output=$session_lines
            elif [ "$remaining" -gt 0 ]; then
                already_output=$remaining
            fi
            _start_session_tail "$sid" "$already_output"
            return $?
        else
            # Completed session -- dump any lines we haven't output yet
            if [ "$remaining" -ge "$session_lines" ]; then
                # Already fully output
                remaining=$((remaining - session_lines))
            else
                # Partially output or not at all
                _dump_remaining_lines "$session_file" "$remaining"
                remaining=0
            fi
        fi
    done

    return 1
}

# Main loop: catch up on existing sessions, then watch for new ones.
main() {
    log_info "Stream transcript started"
    log_info "  Session history: $SESSION_HISTORY"
    log_info "  Output: $OUTPUT_FILE"

    # Wait for the session history file to be created
    while [ ! -f "$SESSION_HISTORY" ]; do
        sleep 1
    done

    log_info "Session history file found"

    # Catch up on any sessions that already exist
    _catch_up_and_tail || true

    # Watch the session history file for new sessions.
    # Use process substitution (not a pipe) so the while loop runs in the
    # main shell and variable updates to _SESSION_TAIL_PID are visible.
    local history_lines
    history_lines=$(_line_count "$SESSION_HISTORY")
    local tail_start=$((history_lines + 1))

    log_info "Watching session history from line $tail_start"

    while read -r new_sid _rest; do
        if [ -z "$new_sid" ]; then
            continue
        fi

        log_info "New session detected: $new_sid"

        # Kill the current session tail -- the old session is done
        _kill_session_tail

        # Start tailing the new session from the beginning
        _start_session_tail "$new_sid" 0 || true
    done < <(stdbuf -oL tail -n "+${tail_start}" -f "$SESSION_HISTORY")
}

main
