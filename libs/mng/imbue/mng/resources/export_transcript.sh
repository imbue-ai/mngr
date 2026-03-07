#!/bin/bash
# Export raw Claude Code conversation JSONL for all sessions.
#
# Outputs the raw .jsonl content for every session ID in chronological order.
# No filtering or formatting is applied -- callers can pipe to jq or grep.
#
# Session IDs are read from $MNG_AGENT_STATE_DIR/claude_session_id_history
# (one per line, format: "session_id source"). If $CLAUDE_CODE_SESSION_ID is
# set and not already in the history, it is appended so the current session's
# transcript is always included (this also covers plain Claude Code sessions
# without the mng agent infrastructure).

set -euo pipefail

_process_session() {
    local session_id="$1"
    local jsonl_file
    jsonl_file=$(find ~/.claude/projects/ -name "$session_id.jsonl" 2>/dev/null | head -1)
    if [ -n "$jsonl_file" ] && [ -f "$jsonl_file" ]; then
        cat "$jsonl_file"
    fi
}

# Collect all session IDs in chronological order from the history file
_SESSION_IDS=()

if [ -n "${MNG_AGENT_STATE_DIR:-}" ] && [ -f "$MNG_AGENT_STATE_DIR/claude_session_id_history" ]; then
    # Each line is "session_id source" -- extract just the session_id (first field)
    while read -r sid _rest; do
        if [ -n "$sid" ]; then
            _SESSION_IDS+=("$sid")
        fi
    done < "$MNG_AGENT_STATE_DIR/claude_session_id_history"
fi

# Ensure the current Claude Code session is included (covers plain sessions
# without mng agent infrastructure, and handles the edge case where the
# SessionStart hook hasn't fired yet for the current session).
if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
    _ALREADY_PRESENT=false
    for sid in "${_SESSION_IDS[@]}"; do
        if [ "$sid" = "$CLAUDE_CODE_SESSION_ID" ]; then
            _ALREADY_PRESENT=true
            break
        fi
    done
    if [ "$_ALREADY_PRESENT" = false ]; then
        _SESSION_IDS+=("$CLAUDE_CODE_SESSION_ID")
    fi
fi

if [ ${#_SESSION_IDS[@]} -eq 0 ]; then
    # No sessions found -- exit silently (not an error, agent may not have started yet)
    exit 0
fi

# Output all session .jsonl files in order
for sid in "${_SESSION_IDS[@]}"; do
    _process_session "$sid"
done
