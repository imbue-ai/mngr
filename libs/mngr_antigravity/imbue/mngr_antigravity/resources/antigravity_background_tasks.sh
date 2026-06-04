#!/usr/bin/env bash
# Background tasks supervisor for antigravity agents.
#
# Runs continuously while the agent's tmux session is alive, supervising:
#   1. Raw transcript streaming: stream_transcript.sh tails agy's per-
#      conversation JSONL transcripts (filtered to conversation IDs that
#      this agent worked on, discovered from the per-agent conversation-ids
#      file the PreInvocation capture hook maintains -- see
#      capture_conversation_id.sh) into
#      $MNGR_AGENT_STATE_DIR/logs/antigravity_transcript/events.jsonl.
#   2. Common transcript conversion (optional): common_transcript.sh
#      converts the raw stream into the agent-agnostic common format at
#      $MNGR_AGENT_STATE_DIR/events/antigravity/common_transcript/events.jsonl.
#      Only launched if the script is present in commands/ (provision()
#      writes it when emit_common_transcript=True).
#
# Restart dead children, clean them up on exit, and dedup via pidfile so
# concurrent re-runs (e.g. agent restart) don't pile up watchers racing
# on the same offset files and output file. Mirrors the structure of
# mngr_claude's claude_background_tasks.sh; agy exposes no
# UserPromptSubmit-style activity hook so the loop body only restarts
# watchers.
#
# Usage: antigravity_background_tasks.sh <tmux_session_name>
#
# Environment:
#   MNGR_AGENT_STATE_DIR  - the agent's state directory (contains commands/)

set -euo pipefail

SESSION_NAME="${1:-}"

if [ -z "$SESSION_NAME" ]; then
    echo "Usage: antigravity_background_tasks.sh <tmux_session_name>" >&2
    exit 1
fi

_MNGR_ANTIGRAVITY_LOCK="/tmp/mngr_antigravity_${SESSION_NAME}.pid"

if [ -f "$_MNGR_ANTIGRAVITY_LOCK" ] && kill -0 "$(cat "$_MNGR_ANTIGRAVITY_LOCK" 2>/dev/null)" 2>/dev/null; then
    exit 0
fi

echo $$ > "$_MNGR_ANTIGRAVITY_LOCK"

mkdir -p "$MNGR_AGENT_STATE_DIR/events"

_MNGR_LOG_TYPE="antigravity_background_tasks"
_MNGR_LOG_SOURCE="logs/antigravity_background_tasks"
_MNGR_LOG_FILE="$MNGR_AGENT_STATE_DIR/events/logs/antigravity_background_tasks/events.jsonl"
# shellcheck source=mngr_log.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_log.sh"

STREAM_SCRIPT="$MNGR_AGENT_STATE_DIR/commands/stream_transcript.sh"
_STREAM_PID=""
if [ -x "$STREAM_SCRIPT" ]; then
    bash "$STREAM_SCRIPT" &
    _STREAM_PID=$!
    log_info "Started raw transcript streaming (PID: $_STREAM_PID)"
fi

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
    rm -f "$_MNGR_ANTIGRAVITY_LOCK"
}
trap _cleanup EXIT

log_info "Background tasks started for session $SESSION_NAME"

# `=` is tmux's exact-match prefix; without it the loop would never exit when
# our session is gone but a prefix-collision sibling is alive.
while tmux has-session -t "=$SESSION_NAME" 2>/dev/null; do
    if [ -n "$_STREAM_PID" ] && ! kill -0 "$_STREAM_PID" 2>/dev/null; then
        log_warn "Raw transcript streamer died, restarting"
        if [ -x "$STREAM_SCRIPT" ]; then
            bash "$STREAM_SCRIPT" &
            _STREAM_PID=$!
            log_info "Restarted raw transcript streamer (PID: $_STREAM_PID)"
        fi
    fi

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
