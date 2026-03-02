#!/bin/bash
# Combined background tasks for Claude agents.
#
# This script runs continuously while the agent's tmux session is alive,
# performing two tasks:
#   1. Activity tracking: updates $MNG_AGENT_STATE_DIR/activity/agent
#      whenever the agent is actively processing (indicated by the
#      $MNG_AGENT_STATE_DIR/active file)
#   2. Transcript export: periodically exports the conversation transcript
#      to $MNG_AGENT_STATE_DIR/logs/claude_transcript/events.jsonl
#
# Usage: claude_background_tasks.sh <tmux_session_name>
#
# Requires environment variables:
#   MNG_AGENT_STATE_DIR  - the agent's state directory
#   MNG_HOST_DIR         - the host data directory (contains commands/)
#
# Uses a pidfile to prevent duplicate instances for the same session.

set -euo pipefail

SESSION_NAME="${1:-}"

if [ -z "$SESSION_NAME" ]; then
    echo "Usage: claude_background_tasks.sh <tmux_session_name>" >&2
    exit 1
fi

# Prevent duplicate instances using a pidfile
_MNG_ACT_LOCK="/tmp/mng_act_${SESSION_NAME}.pid"

if [ -f "$_MNG_ACT_LOCK" ] && kill -0 "$(cat "$_MNG_ACT_LOCK" 2>/dev/null)" 2>/dev/null; then
    exit 0
fi

echo $$ > "$_MNG_ACT_LOCK"
trap 'rm -f "$_MNG_ACT_LOCK"' EXIT

# Ensure required directories exist
mkdir -p "$MNG_AGENT_STATE_DIR/activity"
mkdir -p "$MNG_AGENT_STATE_DIR/logs"

EXPORT_SCRIPT="$MNG_HOST_DIR/commands/export_transcript.sh"

while tmux has-session -t "$SESSION_NAME" 2>/dev/null; do
    # Task 1: Update activity timestamp if agent is actively processing
    if [ -f "$MNG_AGENT_STATE_DIR/active" ]; then
        printf '{"time": %d, "source": "activity_updater"}' \
            "$(($(date +%s) * 1000))" > "$MNG_AGENT_STATE_DIR/activity/agent"
    fi

    # Task 2: Export transcript if the export script is available
    # Uses temp file + mv for atomic replacement so readers never see a truncated file
    if [ -x "$EXPORT_SCRIPT" ]; then
        mkdir -p "$MNG_AGENT_STATE_DIR/logs/claude_transcript"
        _TRANSCRIPT_TMP="$MNG_AGENT_STATE_DIR/logs/claude_transcript/events.jsonl.tmp"
        if "$EXPORT_SCRIPT" > "$_TRANSCRIPT_TMP" 2>/dev/null; then
            mv "$_TRANSCRIPT_TMP" "$MNG_AGENT_STATE_DIR/logs/claude_transcript/events.jsonl"
        else
            rm -f "$_TRANSCRIPT_TMP"
        fi
    fi

    sleep 15
done

rm -f "$_MNG_ACT_LOCK"
