#!/usr/bin/env bash
# PreInvocation hook: mark the agent RUNNING and record the turn's root agent.
#
# agy runs this before each model call (see build_antigravity_hooks_config),
# passing a JSON payload on stdin that carries the conversation id. It touches
# the `active` marker (BaseAgent reads its presence as RUNNING) and, when the
# marker was absent -- i.e. this invocation opens a new turn -- records the
# conversation id as the turn's root in `root_conversation`.
#
# Why record the root: subagents share this same hook and fire their own Stop
# (with `fullyIdle:true`) when they finish, which can arrive while the root
# agent is still working. clear_active_marker_when_idle.sh clears the marker
# only for the root conversation, so it needs to know which one that is. agy
# always runs the root agent's invocation before it spawns any subagent (the
# root touches `active` first), so the conversation that opens a turn -- the one
# seen while `active` is absent -- is always the true root. Re-recording it at
# each turn boundary keeps it correct across /clear, /fork, /switch, and resume.
#
# Marker / root-file names are kept in sync with antigravity_config.py. Never
# writes stdout (agy treats PreInvocation stdout as injected steps); avoids
# `set -e` so a malformed payload can't disrupt agy's loop.

if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "set_active_marker.sh: MNGR_AGENT_STATE_DIR is not set" >&2
    exit 1
fi

marker_file="$MNGR_AGENT_STATE_DIR/active"
root_file="$MNGR_AGENT_STATE_DIR/root_conversation"

payload=$(cat)

# Record the root only at a turn boundary (marker absent), checked before the
# touch below so it reflects the pre-invocation state. POSIX grep/sed only --
# no jq dependency (jq may be absent on remote hosts).
if [ ! -e "$marker_file" ]; then
    conv_id=$(
        printf '%s' "$payload" \
            | grep -oE '"conversationId":"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"' \
            | head -n 1 \
            | sed -E 's/.*:"([0-9a-f-]+)".*/\1/'
    )
    if [ -n "$conv_id" ]; then
        printf '%s' "$conv_id" > "$root_file"
    fi
fi

touch "$marker_file"
