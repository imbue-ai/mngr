#!/usr/bin/env bash
# Clear this agent's `active` lifecycle marker only when agy reports the
# conversation is FULLY idle.
#
# agy fires this as a Stop hook handler (see build_antigravity_hooks_config).
# When the root agent goes idle it runs the Stop hooks and passes a JSON object
# on stdin that includes `"fullyIdle":<bool>` -- true only when the root agent
# AND every subagent / background task it launched have all finished, and
# `false` while async work is still running. agy emits the field explicitly in
# both cases (a not-fully-idle Stop sends `"fullyIdle":false`, not an omitted
# field), and fires Stop again -- a final time with `"fullyIdle":true` -- once
# the async work completes and the root agent goes idle for good. Verified live
# against agy 1.0.5 (a backgrounded shell task produced a `fullyIdle:false`
# Stop followed by a `fullyIdle:true` Stop).
#
# The `active` marker drives BaseAgent's RUNNING/WAITING detection: present =>
# RUNNING, absent => WAITING. PreInvocation touches it before every model call;
# this hook removes it only on a fully-idle Stop. So an agent that goes idle
# while a subagent or backgrounded command is still running keeps reporting
# RUNNING until that work also completes -- at which point agy wakes the root
# agent (a new execution) and, once it too goes idle, fires a final Stop with
# `fullyIdle:true` that clears the marker.
#
# The marker name is kept in sync with ACTIVE_MARKER_FILENAME in
# antigravity_config.py. This script must never write to stdout: agy treats
# Stop-hook stdout as a structured result that can block the stop. It also
# deliberately avoids `set -e`/non-zero exits on the common paths so a
# malformed payload never disrupts agy's execution loop.

# mngr sets MNGR_AGENT_STATE_DIR for every agent process, and agy invokes this
# script through a path that embeds it (`$MNGR_AGENT_STATE_DIR/commands/...`),
# so it is always set in the real hook path -- an unset/empty value means a
# wiring bug, not a tolerable runtime case. Fail loudly (to stderr, never
# stdout) rather than silently removing the marker (which would wrongly flip
# the agent to WAITING).
if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "clear_active_marker_when_idle.sh: MNGR_AGENT_STATE_DIR is not set" >&2
    exit 1
fi

marker_file="$MNGR_AGENT_STATE_DIR/active"

payload=$(cat)

# Clear the marker only when the payload explicitly reports full idleness.
# Match the positive `"fullyIdle":true` form (tolerating insignificant JSON
# whitespace between the key, colon, and value) and leave the marker untouched
# otherwise, so the agent stays RUNNING while async work continues. POSIX
# grep only -- no jq dependency (jq may be absent on remote hosts).
if printf '%s' "$payload" | grep -qE '"fullyIdle"[[:space:]]*:[[:space:]]*true'; then
    rm -f "$marker_file"
fi
