#!/usr/bin/env bash
# Print paths to Claude Code session JSONL files for all sessions.
# Outputs one line per file: "<source>\t<path>"
# where source is one of: mngr_tracked, current, mngr_agent_dir,
# or a subagent variant like mngr_tracked:subagent, current:subagent, etc.
#
# Discovery is controlled by .reviewer/settings.json (under verify_conversation),
# with optional local overrides from .reviewer/settings.local.json.
# If the config file is not present, all toggles default to true.
#
# Session IDs are read from $MNGR_AGENT_STATE_DIR/claude_session_id_history.
# If $MNGR_CLAUDE_SESSION_ID is set and not already in the history, it is
# included in the output so the current session is always covered.
# The agent_dir mode scans $CLAUDE_CONFIG_DIR/projects/ for all .jsonl files.

set -euo pipefail

# shellcheck source=config_utils.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config_utils.sh"

SETTINGS=".reviewer/settings.json"

# Env vars override config file (allows the skill to narrow scope per-invocation)
INCLUDE_TRACKED="${INCLUDE_TRACKED:-$(read_json_config "$SETTINGS" "verify_conversation.include_tracked_sessions" "true")}"
INCLUDE_CURRENT="${INCLUDE_CURRENT:-$(read_json_config "$SETTINGS" "verify_conversation.include_current_session" "true")}"
INCLUDE_AGENT_DIR="${INCLUDE_AGENT_DIR:-$(read_json_config "$SETTINGS" "verify_conversation.include_all_agent_sessions" "true")}"
INCLUDE_SUBAGENTS="${INCLUDE_SUBAGENTS:-$(read_json_config "$SETTINGS" "verify_conversation.include_subagents" "true")}"

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
    local parent_source="$1"
    local jsonl_file="$2"
    local session_dir="${jsonl_file%.jsonl}"
    local subagents_dir="$session_dir/subagents"
    if [ -d "$subagents_dir" ]; then
        for subagent_file in "$subagents_dir"/*.jsonl; do
            [ -f "$subagent_file" ] && _emit "${parent_source}:subagent" "$subagent_file"
        done
    fi
}

# ---------------------------------------------------------------------------
# Helper: resolve a session ID to a .jsonl file path (prints nothing if not found)
# ---------------------------------------------------------------------------
_find_session_file() {
    local session_id="$1"
    local search_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects"
    [ -d "$search_dir" ] || return
    local jsonl_file
    jsonl_file=$(find "$search_dir" -name "$session_id.jsonl" 2>/dev/null | head -1)
    if [ -n "$jsonl_file" ] && [ -f "$jsonl_file" ]; then
        echo "$jsonl_file"
    fi
}

# ---------------------------------------------------------------------------
# 1. Tracked sessions
# ---------------------------------------------------------------------------
_TRACKED_SESSION_IDS=()
declare -A _SEEN_SIDS

if [ "$INCLUDE_TRACKED" = "true" ]; then
    if [ -n "${MNGR_AGENT_STATE_DIR:-}" ] && [ -f "$MNGR_AGENT_STATE_DIR/claude_session_id_history" ]; then
        while read -r sid _rest; do
            if [ -n "$sid" ] && [ -z "${_SEEN_SIDS[$sid]:-}" ]; then
                _TRACKED_SESSION_IDS+=("$sid")
                _SEEN_SIDS[$sid]=1
            fi
        done < "$MNGR_AGENT_STATE_DIR/claude_session_id_history"
    fi

    for sid in "${_TRACKED_SESSION_IDS[@]}"; do
        file=$(_find_session_file "$sid")
        if [ -n "$file" ]; then
            _emit "mngr_tracked" "$file"
            [ "$INCLUDE_SUBAGENTS" = "true" ] && _emit_subagents "mngr_tracked" "$file"
        fi
    done
fi

# ---------------------------------------------------------------------------
# 2. Current session (only if not already emitted via tracked)
# ---------------------------------------------------------------------------
if [ "$INCLUDE_CURRENT" = "true" ] && [ -n "${MNGR_CLAUDE_SESSION_ID:-}" ]; then
    if [ -z "${_SEEN_SIDS[$MNGR_CLAUDE_SESSION_ID]:-}" ]; then
        _SEEN_SIDS[$MNGR_CLAUDE_SESSION_ID]=1
        file=$(_find_session_file "$MNGR_CLAUDE_SESSION_ID")
        if [ -n "$file" ]; then
            _emit "current" "$file"
            [ "$INCLUDE_SUBAGENTS" = "true" ] && _emit_subagents "current" "$file"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 3. Agent dir scan -- find ALL .jsonl files under CLAUDE_CONFIG_DIR/projects/
# ---------------------------------------------------------------------------
if [ "$INCLUDE_AGENT_DIR" = "true" ]; then
    search_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects"
    if [ -d "$search_dir" ]; then
        while IFS= read -r jsonl_file; do
            [ -f "$jsonl_file" ] || continue
            # Skip files inside subagents/ directories (handled separately)
            case "$jsonl_file" in
                */subagents/*) continue ;;
            esac
            _emit "mngr_agent_dir" "$jsonl_file"
            [ "$INCLUDE_SUBAGENTS" = "true" ] && _emit_subagents "mngr_agent_dir" "$jsonl_file"
        done < <(find "$search_dir" -name '*.jsonl' 2>/dev/null | sort)
    fi
fi
