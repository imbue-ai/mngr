#!/usr/bin/env bash
# Background tasks supervisor for opencode agents.
#
# Unlike mngr_antigravity (which supervises both a raw streamer and a
# converter), the opencode RAW transcript is written in-process by the
# OpenCode plugin (resources/mngr_opencode_plugin.ts) as message/part events
# arrive -- so the only thing to supervise here is the common-transcript
# converter, which turns that raw JSONL into the agent-agnostic common format
# at $MNGR_AGENT_STATE_DIR/events/opencode/common_transcript/events.jsonl.
#
# Runs while the agent's tmux session is alive: launch the converter, restart
# it if it dies, clean it up on exit, and dedup via pidfile so concurrent
# re-runs (e.g. agent restart) don't pile up. Mirrors the structure of
# mngr_antigravity's antigravity_background_tasks.sh.
#
# Usage: opencode_background_tasks.sh <tmux_session_name>
#
# Environment:
#   MNGR_AGENT_STATE_DIR  - the agent's state directory (contains commands/)

set -euo pipefail

SESSION_NAME="${1:-}"

if [ -z "$SESSION_NAME" ]; then
    echo "Usage: opencode_background_tasks.sh <tmux_session_name>" >&2
    exit 1
fi

_MNGR_OPENCODE_LOCK="/tmp/mngr_opencode_${SESSION_NAME}.pid"

if [ -f "$_MNGR_OPENCODE_LOCK" ] && kill -0 "$(cat "$_MNGR_OPENCODE_LOCK" 2>/dev/null)" 2>/dev/null; then
    exit 0
fi

echo $$ > "$_MNGR_OPENCODE_LOCK"

mkdir -p "$MNGR_AGENT_STATE_DIR/events"

_MNGR_LOG_TYPE="opencode_background_tasks"
_MNGR_LOG_SOURCE="logs/opencode_background_tasks"
_MNGR_LOG_FILE="$MNGR_AGENT_STATE_DIR/events/logs/opencode_background_tasks/events.jsonl"
# shellcheck source=mngr_log.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_log.sh"

COMMON_TRANSCRIPT_SCRIPT="$MNGR_AGENT_STATE_DIR/commands/opencode_common_transcript.sh"
_COMMON_TRANSCRIPT_PID=""
if [ -x "$COMMON_TRANSCRIPT_SCRIPT" ]; then
    bash "$COMMON_TRANSCRIPT_SCRIPT" &
    _COMMON_TRANSCRIPT_PID=$!
    log_info "Started common transcript converter (PID: $_COMMON_TRANSCRIPT_PID)"
fi

_cleanup() {
    if [ -n "$_COMMON_TRANSCRIPT_PID" ] && kill -0 "$_COMMON_TRANSCRIPT_PID" 2>/dev/null; then
        kill "$_COMMON_TRANSCRIPT_PID" 2>/dev/null
        wait "$_COMMON_TRANSCRIPT_PID" 2>/dev/null || true
    fi
    rm -f "$_MNGR_OPENCODE_LOCK"
}
trap _cleanup EXIT

log_info "Background tasks started for session $SESSION_NAME"

# `=` is tmux's exact-match prefix; without it the loop would never exit when
# our session is gone but a prefix-collision sibling is alive.
while tmux has-session -t "=$SESSION_NAME" 2>/dev/null; do
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
