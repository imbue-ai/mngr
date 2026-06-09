#!/usr/bin/env bash
# Stop hook: clear the `active` lifecycle marker when the ROOT agent stops.
#
# codex runs this when a turn ends, passing a JSON payload on stdin with the
# session id (verified live: it also carries last_assistant_message,
# stop_hook_active, etc.). It removes the `active` marker (so BaseAgent reports
# WAITING) only for the root session recorded by set_active_marker.sh in
# `codex_root_session`.
#
# Unlike antigravity -- where subagents share the Stop hook and a `fullyIdle`
# flag distinguishes the root's final Stop from a subagent's interim one --
# codex's Stop is ALREADY root-only: Task-style subagents fire a distinct
# SubagentStop event that mngr deliberately does not hook, so a subagent
# finishing never reaches this script. There is therefore NO fullyIdle flag to
# check; the only thing to guard against is a *separate* nested/recursive codex
# process that shares this CODEX_HOME (and thus these hooks) and whose Stop would
# carry a different session id.
#
# The clear is gated on the recorded root session: remove the marker only when
# the payload's session id matches the recorded root (the root's own Stop). A
# Stop carrying any other session id (a nested codex) leaves the marker, so the
# root keeps reporting RUNNING. As a liveness fallback, if no root session was
# recorded (empty or absent file), the marker is cleared anyway, so a failure to
# record the root can never strand the agent in RUNNING forever.
#
# Marker / root-file names are kept in sync with codex_config.py. Never writes
# stdout (codex can treat Stop-hook stdout as a result that blocks the stop);
# avoids `set -e` so a malformed payload can't disrupt codex's loop.

if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "clear_active_marker.sh: MNGR_AGENT_STATE_DIR is not set" >&2
    exit 1
fi

marker_file="$MNGR_AGENT_STATE_DIR/active"
root_file="$MNGR_AGENT_STATE_DIR/codex_root_session"

payload=$(cat)

# Extract this Stop's session id. POSIX grep/sed only -- no jq (it may be absent
# on remote hosts).
session_id=$(
    printf '%s' "$payload" \
        | grep -oE '"session_id":"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"' \
        | head -n 1 \
        | sed -E 's/.*:"([0-9a-f-]+)".*/\1/'
)

# The root session recorded at the turn boundary (may be empty/absent).
root_session=""
if [ -f "$root_file" ]; then
    root_session=$(cat "$root_file" 2>/dev/null)
fi

# Clear when this is the root's Stop, or (liveness fallback) when no root was
# recorded at all. A different session id (a nested codex) leaves the marker.
if [ -z "$root_session" ] || [ "$session_id" = "$root_session" ]; then
    rm -f "$marker_file"
fi
