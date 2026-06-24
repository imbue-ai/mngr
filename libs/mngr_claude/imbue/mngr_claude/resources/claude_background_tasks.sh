#!/usr/bin/env bash
# Combined background tasks for Claude agents.
#
# This script runs continuously while the agent's tmux session is alive,
# performing these tasks:
#   1. Activity tracking: updates $MNGR_AGENT_STATE_DIR/activity/agent
#      whenever the agent is actively processing (indicated by the
#      $MNGR_AGENT_STATE_DIR/active file)
#   2. Transcript streaming: launches stream_transcript.sh which watches
#      all session JSONL files and streams new lines to
#      $MNGR_AGENT_STATE_DIR/logs/claude_transcript/events.jsonl
#   3. Common transcript (optional): launches common_transcript.sh, which
#      converts the raw transcript into an agent-agnostic common format at
#      $MNGR_AGENT_STATE_DIR/events/claude/common_transcript/events.jsonl,
#      only if that script is present in commands/. ClaudeAgent.provision()
#      skips writing the converter when emit_common_transcript=False, so
#      disabled-emit takes effect simply via the on-disk -x check.
#
# Usage: claude_background_tasks.sh <tmux_session_name> [primary_window_name]
#
# primary_window_name is the agent's primary tmux window name (config
# tmux.primary_window_name, default "agent"); it is passed through to
# stream_snapshot.py so pane capture targets the window by name rather than the
# literal :0 index (base-index agnostic).
#
# Requires environment variables:
#   MNGR_AGENT_STATE_DIR  - the agent's state directory (contains commands/)
#
# Uses a pidfile to prevent duplicate instances for the same session.

set -euo pipefail

SESSION_NAME="${1:-}"
# Default to "agent" when omitted so older callers keep working.
PRIMARY_WINDOW_NAME="${2:-agent}"

if [ -z "$SESSION_NAME" ]; then
    echo "Usage: claude_background_tasks.sh <tmux_session_name> [primary_window_name]" >&2
    exit 1
fi

# Prevent duplicate instances using a pidfile
_MNGR_ACT_LOCK="/tmp/mngr_act_${SESSION_NAME}.pid"

if [ -f "$_MNGR_ACT_LOCK" ] && kill -0 "$(cat "$_MNGR_ACT_LOCK" 2>/dev/null)" 2>/dev/null; then
    exit 0
fi

echo $$ > "$_MNGR_ACT_LOCK"

# Ensure required directories exist
mkdir -p "$MNGR_AGENT_STATE_DIR/activity"
mkdir -p "$MNGR_AGENT_STATE_DIR/events"

# Configure and source the shared logging library
_MNGR_LOG_TYPE="claude_background_tasks"
_MNGR_LOG_SOURCE="logs/claude_background_tasks"
_MNGR_LOG_FILE="$MNGR_AGENT_STATE_DIR/events/logs/claude_background_tasks/events.jsonl"
# shellcheck source=mngr_log.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_log.sh"

# Start transcript streaming in the background
STREAM_SCRIPT="$MNGR_AGENT_STATE_DIR/commands/stream_transcript.sh"
_STREAM_PID=""
if [ -x "$STREAM_SCRIPT" ]; then
    bash "$STREAM_SCRIPT" &
    _STREAM_PID=$!
    log_info "Started transcript streaming (PID: $_STREAM_PID)"
fi

# Optionally start common transcript conversion in the background. The
# converter script is only on disk when ClaudeAgent.provision() decided to
# emit the common transcript, so this -x check is the single gate.
COMMON_TRANSCRIPT_SCRIPT="$MNGR_AGENT_STATE_DIR/commands/common_transcript.sh"
_COMMON_TRANSCRIPT_PID=""
if [ -x "$COMMON_TRANSCRIPT_SCRIPT" ]; then
    bash "$COMMON_TRANSCRIPT_SCRIPT" &
    _COMMON_TRANSCRIPT_PID=$!
    log_info "Started common transcript converter (PID: $_COMMON_TRANSCRIPT_PID)"
fi

# Optionally start the response-streaming watcher. Like the common transcript
# converter, this script is only on disk when ClaudeAgent.provision() decided to
# enable streaming (streaming_snapshot_interval_seconds > 0), so its presence is
# the single gate. It reads the poll interval from the stream_interval file under
# $MNGR_AGENT_STATE_DIR/plugin/claude/ (env-var propagation into this subshell is
# unreliable, so the interval is passed via a file instead).
STREAM_SNAPSHOT_SCRIPT="$MNGR_AGENT_STATE_DIR/commands/stream_snapshot.py"
_STREAM_SNAPSHOT_PID=""
if [ -f "$STREAM_SNAPSHOT_SCRIPT" ]; then
    python3 "$STREAM_SNAPSHOT_SCRIPT" "$SESSION_NAME" "$PRIMARY_WINDOW_NAME" &
    _STREAM_SNAPSHOT_PID=$!
    log_info "Started response stream snapshot watcher (PID: $_STREAM_SNAPSHOT_PID)"
fi

_cleanup() {
    # Stop the transcript streaming process
    if [ -n "$_STREAM_PID" ] && kill -0 "$_STREAM_PID" 2>/dev/null; then
        kill "$_STREAM_PID" 2>/dev/null
        wait "$_STREAM_PID" 2>/dev/null || true
    fi
    # Stop the common transcript converter process
    if [ -n "$_COMMON_TRANSCRIPT_PID" ] && kill -0 "$_COMMON_TRANSCRIPT_PID" 2>/dev/null; then
        kill "$_COMMON_TRANSCRIPT_PID" 2>/dev/null
        wait "$_COMMON_TRANSCRIPT_PID" 2>/dev/null || true
    fi
    # Stop the response stream snapshot watcher
    if [ -n "$_STREAM_SNAPSHOT_PID" ] && kill -0 "$_STREAM_SNAPSHOT_PID" 2>/dev/null; then
        kill "$_STREAM_SNAPSHOT_PID" 2>/dev/null
        wait "$_STREAM_SNAPSHOT_PID" 2>/dev/null || true
    fi
    rm -f "$_MNGR_ACT_LOCK"
}
trap _cleanup EXIT

log_info "Background tasks started for session $SESSION_NAME"

# `=` is tmux's exact-match prefix; without it the loop would never exit when
# our session is gone but a prefix-collision sibling is alive.
while tmux has-session -t "=$SESSION_NAME" 2>/dev/null; do
    # Update activity timestamp if agent is actively processing
    if [ -f "$MNGR_AGENT_STATE_DIR/active" ]; then
        printf '{"time": %d, "source": "activity_updater"}' \
            "$(($(date +%s) * 1000))" > "$MNGR_AGENT_STATE_DIR/activity/agent"
    fi

    # Restart transcript streaming if it died unexpectedly
    if [ -n "$_STREAM_PID" ] && ! kill -0 "$_STREAM_PID" 2>/dev/null; then
        log_warn "Transcript streaming process died, restarting"
        if [ -x "$STREAM_SCRIPT" ]; then
            bash "$STREAM_SCRIPT" &
            _STREAM_PID=$!
            log_info "Restarted transcript streaming (PID: $_STREAM_PID)"
        fi
    fi

    # Restart common transcript converter if it died unexpectedly
    if [ -n "$_COMMON_TRANSCRIPT_PID" ] && ! kill -0 "$_COMMON_TRANSCRIPT_PID" 2>/dev/null; then
        log_warn "Common transcript converter process died, restarting"
        if [ -x "$COMMON_TRANSCRIPT_SCRIPT" ]; then
            bash "$COMMON_TRANSCRIPT_SCRIPT" &
            _COMMON_TRANSCRIPT_PID=$!
            log_info "Restarted common transcript converter (PID: $_COMMON_TRANSCRIPT_PID)"
        fi
    fi

    # Restart the response stream snapshot watcher if it died unexpectedly
    if [ -n "$_STREAM_SNAPSHOT_PID" ] && ! kill -0 "$_STREAM_SNAPSHOT_PID" 2>/dev/null; then
        log_warn "Response stream snapshot watcher died, restarting"
        if [ -f "$STREAM_SNAPSHOT_SCRIPT" ]; then
            python3 "$STREAM_SNAPSHOT_SCRIPT" "$SESSION_NAME" "$PRIMARY_WINDOW_NAME" &
            _STREAM_SNAPSHOT_PID=$!
            log_info "Restarted response stream snapshot watcher (PID: $_STREAM_SNAPSHOT_PID)"
        fi
    fi

    sleep 15
done

log_info "Background tasks finished for session $SESSION_NAME (session ended)"
