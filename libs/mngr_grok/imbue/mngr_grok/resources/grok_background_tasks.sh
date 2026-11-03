#!/usr/bin/env bash
# Background tasks supervisor for grok agents.
#
# While the agent's tmux session is alive, every poll cycle it writes activity/agent while
# the `active` marker exists and starts each child script in commands/ that is not running.
# One supervisor runs per agent; its children stop with it. Its own and its children's
# stray output goes to logs/grok_background_tasks.log.
#
# Usage: grok_background_tasks.sh <tmux_session_name>
#
# Environment:
#   MNGR_AGENT_STATE_DIR               - the agent's state directory (contains commands/)
#   MNGR_AGENT_ID                      - the agent's id; keys the pidfile (falls back to the session name)
#   TMUX_PANE                          - set by tmux in the agent's pane; identifies the session
#   MNGR_GROK_SUPERVISOR_POLL_SECONDS  - seconds between cycles

set -euo pipefail

SESSION_NAME="${1:-}"

if [ -z "$SESSION_NAME" ]; then
    echo "Usage: grok_background_tasks.sh <tmux_session_name>" >&2
    exit 1
fi

POLL_SECONDS="${MNGR_GROK_SUPERVISOR_POLL_SECONDS:-15}"
_MNGR_GROK_PIDFILE="/tmp/mngr_grok_${MNGR_AGENT_ID:-$SESSION_NAME}.pid"

# A recorded pid counts only while it is still this script, not a process that reused the pid.
_is_recorded_supervisor_alive() {
    local recorded_pid
    recorded_pid=$(cat "$_MNGR_GROK_PIDFILE" 2>/dev/null || true)
    [ -n "$recorded_pid" ] && kill -0 "$recorded_pid" 2>/dev/null || return 1
    command -v ps >/dev/null 2>&1 || return 0
    case "$(ps -p "$recorded_pid" -o args= 2>/dev/null || true)" in
        *grok_background_tasks.sh*) return 0 ;;
        *) return 1 ;;
    esac
}

if [ -f "$_MNGR_GROK_PIDFILE" ] && _is_recorded_supervisor_alive; then
    exit 0
fi

echo $$ > "$_MNGR_GROK_PIDFILE"

mkdir -p "$MNGR_AGENT_STATE_DIR/activity" "$MNGR_AGENT_STATE_DIR/events" "$MNGR_AGENT_STATE_DIR/logs"
exec >>"$MNGR_AGENT_STATE_DIR/logs/grok_background_tasks.log" 2>&1

_MNGR_LOG_TYPE="grok_background_tasks"
_MNGR_LOG_SOURCE="logs/grok_background_tasks"
_MNGR_LOG_FILE="$MNGR_AGENT_STATE_DIR/events/logs/grok_background_tasks/events.jsonl"
# shellcheck source=mngr_log.sh
source "$MNGR_AGENT_STATE_DIR/commands/mngr_log.sh"

# Liveness follows the tmux session id, which survives a rename: the agent's own pane
# ($TMUX_PANE) first, then an exact-name lookup for a launch from outside that pane.
SESSION_ID=""
if [ -n "${TMUX_PANE:-}" ]; then
    SESSION_ID=$(tmux display-message -p -t "$TMUX_PANE" '#{session_id}' 2>/dev/null || true)
fi
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(tmux display-message -p -t "=$SESSION_NAME:" '#{session_id}' 2>/dev/null || true)
fi

STREAM_SCRIPT="$MNGR_AGENT_STATE_DIR/commands/stream_transcript.sh"
COMMON_TRANSCRIPT_SCRIPT="$MNGR_AGENT_STATE_DIR/commands/common_transcript.sh"
USAGE_SCRIPT="$MNGR_AGENT_STATE_DIR/commands/grok_usage.sh"
_STREAM_PID=""
_COMMON_TRANSCRIPT_PID=""
_USAGE_PID=""

# Children are gated on -f, since this supervisor can start before provisioning sets the
# executable bit. One function per child: bash 3.2 has no namerefs.
_start_stream_if_needed() {
    if [ -n "$_STREAM_PID" ] && kill -0 "$_STREAM_PID" 2>/dev/null; then return; fi
    if [ -f "$STREAM_SCRIPT" ]; then
        bash "$STREAM_SCRIPT" &
        _STREAM_PID=$!
        log_info "Started raw transcript streaming (PID: $_STREAM_PID)"
    fi
}
_start_common_if_needed() {
    if [ -n "$_COMMON_TRANSCRIPT_PID" ] && kill -0 "$_COMMON_TRANSCRIPT_PID" 2>/dev/null; then return; fi
    if [ -f "$COMMON_TRANSCRIPT_SCRIPT" ]; then
        bash "$COMMON_TRANSCRIPT_SCRIPT" &
        _COMMON_TRANSCRIPT_PID=$!
        log_info "Started common transcript converter (PID: $_COMMON_TRANSCRIPT_PID)"
    fi
}
_start_usage_if_needed() {
    if [ -n "$_USAGE_PID" ] && kill -0 "$_USAGE_PID" 2>/dev/null; then return; fi
    if [ -f "$USAGE_SCRIPT" ]; then
        bash "$USAGE_SCRIPT" &
        _USAGE_PID=$!
        log_info "Started usage writer (PID: $_USAGE_PID)"
    fi
}

_stop_child() {
    local child_pid="$1"
    if [ -n "$child_pid" ] && kill -0 "$child_pid" 2>/dev/null; then
        kill "$child_pid" 2>/dev/null || true
        wait "$child_pid" 2>/dev/null || true
    fi
}

_cleanup() {
    _stop_child "$_STREAM_PID"
    _stop_child "$_COMMON_TRANSCRIPT_PID"
    _stop_child "$_USAGE_PID"
    rm -f "$_MNGR_GROK_PIDFILE"
}
trap _cleanup EXIT

log_info "Background tasks started for session $SESSION_NAME"

while [ -n "$SESSION_ID" ] && tmux has-session -t "$SESSION_ID" 2>/dev/null; do
    if [ -f "$MNGR_AGENT_STATE_DIR/active" ]; then
        printf '{"time": %d, "source": "activity_updater"}' \
            "$(($(date +%s) * 1000))" > "$MNGR_AGENT_STATE_DIR/activity/agent"
    fi
    _start_stream_if_needed
    _start_common_if_needed
    _start_usage_if_needed
    sleep "$POLL_SECONDS"
done

log_info "Background tasks finished for session $SESSION_NAME (session ended)"
