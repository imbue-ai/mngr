#!/usr/bin/env bash
# Background tasks supervisor for gemini agents.
#
# This script runs continuously while the agent's tmux session is alive,
# supervising the watchers that capture gemini transcripts:
#   1. Raw transcript streaming: launches stream_transcript.sh which tails
#      gemini's session JSONL files (filtered by .project_root) into
#      $MNGR_AGENT_STATE_DIR/logs/gemini_transcript/events.jsonl. Always
#      launched -- the raw transcript is required by HasTranscriptMixin.
#   2. Common transcript conversion (optional): launches common_transcript.sh,
#      which converts the raw stream into the agent-agnostic common format at
#      $MNGR_AGENT_STATE_DIR/events/gemini/common_transcript/events.jsonl,
#      only if that script is present in commands/. GeminiAgent.provision()
#      skips writing the converter when emit_common_transcript=False, so
#      disabled-emit takes effect simply via the on-disk -x check.
#
# Restart dead children, clean them up on exit, and dedup via pidfile so
# concurrent re-runs (eg. agent restart) don't pile up watchers racing on
# the same offset files and output file. Mirrors the structure of
# claude_background_tasks.sh; gemini has no activity-tracker equivalent so
# the loop body only restarts watchers.
#
# Usage: gemini_background_tasks.sh <tmux_session_name>
#
# Requires environment variables:
#   MNGR_AGENT_STATE_DIR  - the agent's state directory (contains commands/)

set -euo pipefail

SESSION_NAME="${1:-}"

if [ -z "$SESSION_NAME" ]; then
    echo "Usage: gemini_background_tasks.sh <tmux_session_name>" >&2
    exit 1
fi

# Prevent duplicate instances using a pidfile keyed on the tmux session name.
_MNGR_GEMINI_LOCK="/tmp/mngr_gemini_${SESSION_NAME}.pid"

if [ -f "$_MNGR_GEMINI_LOCK" ] && kill -0 "$(cat "$_MNGR_GEMINI_LOCK" 2>/dev/null)" 2>/dev/null; then
    exit 0
fi

echo $$ > "$_MNGR_GEMINI_LOCK"

mkdir -p "$MNGR_AGENT_STATE_DIR/events"

# Configure and source the shared logging library
_MNGR_LOG_TYPE="gemini_background_tasks"
_MNGR_LOG_SOURCE="logs/gemini_background_tasks"
_MNGR_LOG_FILE="$MNGR_AGENT_STATE_DIR/events/logs/gemini_background_tasks/events.jsonl"
# shellcheck source=mngr_log.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_log.sh"

STREAM_SCRIPT="$MNGR_AGENT_STATE_DIR/commands/stream_transcript.sh"
_STREAM_PID=""
if [ -x "$STREAM_SCRIPT" ]; then
    bash "$STREAM_SCRIPT" &
    _STREAM_PID=$!
    log_info "Started raw transcript streaming (PID: $_STREAM_PID)"
fi

# Optionally start the common transcript converter. Provisioned to disk
# only when GeminiAgent.provision() decides to emit the common transcript,
# so this -x check is the single gate.
COMMON_TRANSCRIPT_SCRIPT="$MNGR_AGENT_STATE_DIR/commands/common_transcript.sh"
_COMMON_TRANSCRIPT_PID=""
if [ -x "$COMMON_TRANSCRIPT_SCRIPT" ]; then
    bash "$COMMON_TRANSCRIPT_SCRIPT" &
    _COMMON_TRANSCRIPT_PID=$!
    log_info "Started common transcript converter (PID: $_COMMON_TRANSCRIPT_PID)"
fi

_cleanup() {
    if [ -n "$_STREAM_PID" ] && kill -0 "$_STREAM_PID" 2>/dev/null; then
        kill "$_STREAM_PID" 2>/dev/null
        wait "$_STREAM_PID" 2>/dev/null || true
    fi
    if [ -n "$_COMMON_TRANSCRIPT_PID" ] && kill -0 "$_COMMON_TRANSCRIPT_PID" 2>/dev/null; then
        kill "$_COMMON_TRANSCRIPT_PID" 2>/dev/null
        wait "$_COMMON_TRANSCRIPT_PID" 2>/dev/null || true
    fi
    rm -f "$_MNGR_GEMINI_LOCK"
}
trap _cleanup EXIT

log_info "Background tasks started for session $SESSION_NAME"

# The leading `=` forces tmux exact-session matching. Without it, tmux falls back
# to session-name prefix matching, so this loop would never exit when our session
# is gone but a sibling session whose name shares this name as a prefix is still
# alive (matches TmuxSessionTarget.as_shell_arg() on the Python side).
while tmux has-session -t "=$SESSION_NAME" 2>/dev/null; do
    # Restart raw transcript streamer if it died unexpectedly
    if [ -n "$_STREAM_PID" ] && ! kill -0 "$_STREAM_PID" 2>/dev/null; then
        log_warn "Raw transcript streamer died, restarting"
        if [ -x "$STREAM_SCRIPT" ]; then
            bash "$STREAM_SCRIPT" &
            _STREAM_PID=$!
            log_info "Restarted raw transcript streamer (PID: $_STREAM_PID)"
        fi
    fi

    # Restart common transcript converter if it died unexpectedly
    if [ -n "$_COMMON_TRANSCRIPT_PID" ] && ! kill -0 "$_COMMON_TRANSCRIPT_PID" 2>/dev/null; then
        log_warn "Common transcript converter died, restarting"
        if [ -x "$COMMON_TRANSCRIPT_SCRIPT" ]; then
            bash "$COMMON_TRANSCRIPT_SCRIPT" &
            _COMMON_TRANSCRIPT_PID=$!
            log_info "Restarted common transcript converter (PID: $_COMMON_TRANSCRIPT_PID)"
        fi
    fi

    sleep 15
done

log_info "Background tasks finished for session $SESSION_NAME (session ended)"
