#!/usr/bin/env bash
# Stop hook: clear the `active` lifecycle marker only when the ROOT agent is
# fully idle (not when a subagent it launched finishes).
#
# agy runs the Stop hooks each time any conversation -- the root agent OR a
# subagent it launched -- goes idle, passing a JSON payload on stdin with the
# conversation id and `"fullyIdle":<bool>`. Subagents share this hook and fire
# their own `"fullyIdle":true` Stop when they finish, which can arrive while the
# root agent is still working, so clearing on any `fullyIdle:true` would wrongly
# flip the agent to WAITING mid-turn. agy emits `fullyIdle` explicitly (an
# interim Stop sends `false`, the final one sends `true`; both verified live
# against agy 1.0.5).
#
# set_active_marker.sh records the turn's root conversation in
# `root_conversation`. This hook removes the marker only when the payload's
# conversation id matches that root AND reports `"fullyIdle":true` -- the root's
# final, everything-done Stop. Any other Stop (a subagent, an interim
# `fullyIdle:false`, or an unparseable payload) leaves the marker, so the agent
# keeps reporting RUNNING. As a liveness fallback, if no root has been recorded
# yet, a `fullyIdle:true` Stop still clears the marker (so a failure to record
# the root can never strand the agent in RUNNING forever).
#
# Marker / root-file names are kept in sync with antigravity_config.py. Never
# writes stdout (agy treats Stop-hook stdout as a result that can block the
# stop); avoids `set -e` so a malformed payload can't disrupt agy's loop.

if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "clear_active_marker_when_idle.sh: MNGR_AGENT_STATE_DIR is not set" >&2
    exit 1
fi

marker_file="$MNGR_AGENT_STATE_DIR/active"
root_file="$MNGR_AGENT_STATE_DIR/root_conversation"

payload=$(cat)

# Only a fully-idle Stop can clear the marker. Match the positive
# `"fullyIdle":true` form (tolerating JSON whitespace); leave the marker for
# `false`, an absent field, or garbage. POSIX grep only -- no jq (it may be
# absent on remote hosts).
if ! printf '%s' "$payload" | grep -qE '"fullyIdle"[[:space:]]*:[[:space:]]*true'; then
    exit 0
fi

# ...and only for the root conversation, so a subagent's own fully-idle Stop
# does not flip the still-working root agent to WAITING.
root_conv=""
if [ -f "$root_file" ]; then
    root_conv=$(cat "$root_file" 2>/dev/null)
fi
conv_id=$(
    printf '%s' "$payload" \
        | grep -oE '"conversationId":"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"' \
        | head -n 1 \
        | sed -E 's/.*:"([0-9a-f-]+)".*/\1/'
)

# Clear when this is the root's Stop, or (liveness fallback) when no root has
# been recorded at all.
if [ -z "$root_conv" ] || [ "$conv_id" = "$root_conv" ]; then
    rm -f "$marker_file"
fi
