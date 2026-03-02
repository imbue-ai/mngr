#!/bin/bash
# Export raw Claude Code conversation JSONL for all sessions.
#
# Outputs the raw .jsonl content for every session ID in chronological order.
# No filtering or formatting is applied -- callers can pipe to jq or grep.
#
# Requires:
#   MNG_AGENT_STATE_DIR  - the agent state directory (contains claude_session_id_history)
#
# Session IDs are read from $MNG_AGENT_STATE_DIR/claude_session_id_history
# (one per line, format: "session_id source"). Falls back to
# $MNG_AGENT_STATE_DIR/claude_session_id or $MAIN_CLAUDE_SESSION_ID if the
# history file is missing.

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

# Fall back to single current session ID if no history available
if [ ${#_SESSION_IDS[@]} -eq 0 ]; then
    _FALLBACK_SID="${MAIN_CLAUDE_SESSION_ID:-}"
    if [ -n "${MNG_AGENT_STATE_DIR:-}" ] && [ -f "$MNG_AGENT_STATE_DIR/claude_session_id" ]; then
        _MNG_READ_SID=$(cat "$MNG_AGENT_STATE_DIR/claude_session_id")
        if [ -n "$_MNG_READ_SID" ]; then
            _FALLBACK_SID="$_MNG_READ_SID"
        fi
    fi
    if [ -n "$_FALLBACK_SID" ]; then
        _SESSION_IDS+=("$_FALLBACK_SID")
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
