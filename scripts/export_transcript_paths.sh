#!/bin/bash
# Print paths to Claude Code session JSONL files for all sessions.
# Outputs one line per file: "<source>\t<path>"
# where source is one of: tracked, current, agent_dir, subagent
#
# Discovery is controlled by .reviews/config/verify-conversation.toml.
# If the config file is missing, all toggles default to true.
#
# Session IDs are read from $MNG_AGENT_STATE_DIR/claude_session_id_history.
# If $MNG_CLAUDE_SESSION_ID is set and not already in the history, it is
# appended so the current session is always included.
# The agent_dir mode scans $CLAUDE_CONFIG_DIR/projects/ for all .jsonl files.

set -euo pipefail

CONFIG_FILE=".reviews/config/verify-conversation.toml"

# ---------------------------------------------------------------------------
# Config reader (simple grep/sed, same approach as stop_hook_autofix.sh)
# ---------------------------------------------------------------------------
_read_config() {
    local key="$1"
    local default="$2"
    if [ -f "$CONFIG_FILE" ]; then
        local val
        val=$(grep "^${key} " "$CONFIG_FILE" 2>/dev/null | head -1 | sed 's/^[^=]*= *//' | sed 's/^"//;s/"$//')
        if [ -n "$val" ]; then
            echo "$val"
            return
        fi
    fi
    echo "$default"
}

INCLUDE_TRACKED=$(_read_config "include_tracked_sessions" "true")
INCLUDE_CURRENT=$(_read_config "include_current_session" "true")
INCLUDE_AGENT_DIR=$(_read_config "include_all_agent_sessions" "true")
INCLUDE_SUBAGENTS=$(_read_config "include_subagents" "true")

# ---------------------------------------------------------------------------
# Track emitted paths to avoid duplicates
# ---------------------------------------------------------------------------
declare -A _EMITTED

_emit() {
    local source="$1"
    local path="$2"
    if [ -z "${_EMITTED[$path]:-}" ]; then
        printf '%s\t%s\n' "$source" "$path"
        _EMITTED[$path]=1
    fi
}

_emit_subagents() {
    local jsonl_file="$1"
    local session_dir="${jsonl_file%.jsonl}"
    local subagents_dir="$session_dir/subagents"
    if [ -d "$subagents_dir" ]; then
        for subagent_file in "$subagents_dir"/*.jsonl; do
            [ -f "$subagent_file" ] && _emit "subagent" "$subagent_file"
        done
    fi
}

# ---------------------------------------------------------------------------
# Helper: resolve a session ID to a .jsonl file path (prints nothing if not found)
# ---------------------------------------------------------------------------
_find_session_file() {
    local session_id="$1"
    local jsonl_file
    for search_dir in "${CLAUDE_CONFIG_DIR:-}"/projects/ ~/.claude/projects/; do
        [ -d "$search_dir" ] || continue
        jsonl_file=$(find "$search_dir" -name "$session_id.jsonl" 2>/dev/null | head -1)
        if [ -n "$jsonl_file" ] && [ -f "$jsonl_file" ]; then
            echo "$jsonl_file"
            return
        fi
    done
}

# ---------------------------------------------------------------------------
# 1. Tracked sessions
# ---------------------------------------------------------------------------
_TRACKED_SESSION_IDS=()
declare -A _SEEN_SIDS

if [ "$INCLUDE_TRACKED" = "true" ]; then
    if [ -n "${MNG_AGENT_STATE_DIR:-}" ] && [ -f "$MNG_AGENT_STATE_DIR/claude_session_id_history" ]; then
        while read -r sid _rest; do
            if [ -n "$sid" ] && [ -z "${_SEEN_SIDS[$sid]:-}" ]; then
                _TRACKED_SESSION_IDS+=("$sid")
                _SEEN_SIDS[$sid]=1
            fi
        done < "$MNG_AGENT_STATE_DIR/claude_session_id_history"
    fi

    for sid in "${_TRACKED_SESSION_IDS[@]}"; do
        file=$(_find_session_file "$sid")
        if [ -n "$file" ]; then
            _emit "tracked" "$file"
            [ "$INCLUDE_SUBAGENTS" = "true" ] && _emit_subagents "$file"
        fi
    done
fi

# ---------------------------------------------------------------------------
# 2. Current session (only if not already emitted via tracked)
# ---------------------------------------------------------------------------
if [ "$INCLUDE_CURRENT" = "true" ] && [ -n "${MNG_CLAUDE_SESSION_ID:-}" ]; then
    if [ -z "${_SEEN_SIDS[$MNG_CLAUDE_SESSION_ID]:-}" ]; then
        _SEEN_SIDS[$MNG_CLAUDE_SESSION_ID]=1
        file=$(_find_session_file "$MNG_CLAUDE_SESSION_ID")
        if [ -n "$file" ]; then
            _emit "current" "$file"
            [ "$INCLUDE_SUBAGENTS" = "true" ] && _emit_subagents "$file"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 3. Agent dir scan -- find ALL .jsonl files under CLAUDE_CONFIG_DIR/projects/
# ---------------------------------------------------------------------------
if [ "$INCLUDE_AGENT_DIR" = "true" ]; then
    for search_dir in "${CLAUDE_CONFIG_DIR:-}"/projects/ ~/.claude/projects/; do
        [ -d "$search_dir" ] || continue
        while IFS= read -r jsonl_file; do
            [ -f "$jsonl_file" ] || continue
            # Skip files inside subagents/ directories (handled separately)
            case "$jsonl_file" in
                */subagents/*) continue ;;
            esac
            _emit "agent_dir" "$jsonl_file"
            [ "$INCLUDE_SUBAGENTS" = "true" ] && _emit_subagents "$jsonl_file"
        done < <(find "$search_dir" -name '*.jsonl' 2>/dev/null | sort)
    done
fi
