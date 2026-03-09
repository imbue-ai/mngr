#!/bin/bash
# Print paths to Claude Code session JSONL files for all sessions.
# Outputs one absolute file path per line, in chronological order.
#
# Session IDs are read from $MNG_AGENT_STATE_DIR/claude_session_id_history.
# If $MNG_CLAUDE_SESSION_ID is set and not already in the history, it is
# appended so the current session is always included.

set -euo pipefail

_find_session_file() {
    local session_id="$1"
    local jsonl_file
    jsonl_file=$(find ~/.claude/projects/ -name "$session_id.jsonl" 2>/dev/null | head -1)
    if [ -n "$jsonl_file" ] && [ -f "$jsonl_file" ]; then
        echo "$jsonl_file"
    fi
}

# Collect all session IDs in chronological order from the history file
_SESSION_IDS=()

if [ -n "${MNG_AGENT_STATE_DIR:-}" ] && [ -f "$MNG_AGENT_STATE_DIR/claude_session_id_history" ]; then
    while read -r sid _rest; do
        if [ -n "$sid" ]; then
            _SESSION_IDS+=("$sid")
        fi
    done < "$MNG_AGENT_STATE_DIR/claude_session_id_history"
fi

# Ensure the current Claude Code session is included
if [ -n "${MNG_CLAUDE_SESSION_ID:-}" ]; then
    _ALREADY_PRESENT=false
    for sid in "${_SESSION_IDS[@]}"; do
        if [ "$sid" = "$MNG_CLAUDE_SESSION_ID" ]; then
            _ALREADY_PRESENT=true
            break
        fi
    done
    if [ "$_ALREADY_PRESENT" = false ]; then
        _SESSION_IDS+=("$MNG_CLAUDE_SESSION_ID")
    fi
fi

if [ ${#_SESSION_IDS[@]} -eq 0 ]; then
    exit 0
fi

for sid in "${_SESSION_IDS[@]}"; do
    _find_session_file "$sid"
done
