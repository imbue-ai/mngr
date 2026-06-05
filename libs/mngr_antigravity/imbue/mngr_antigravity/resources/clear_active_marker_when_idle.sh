#!/usr/bin/env bash
# Clear this agent's `active` lifecycle marker, but only when agy reports the
# conversation is FULLY idle.
#
# agy runs this Stop hook each time the root agent goes idle (see
# build_antigravity_hooks_config), passing a JSON payload on stdin with
# `"fullyIdle":<bool>` -- true only once every subagent / background task it
# launched has also finished. So a turn that backgrounds work yields an interim
# `"fullyIdle":false` Stop then a final `"fullyIdle":true` Stop (both verified
# live against agy 1.0.5). The `active` marker drives BaseAgent's
# RUNNING/WAITING detection (present => RUNNING); PreInvocation touches it and
# this hook removes it only on the fully-idle Stop, so the agent stays RUNNING
# until backgrounded work completes.
#
# Keep the marker name in sync with ACTIVE_MARKER_FILENAME in
# antigravity_config.py. Never writes stdout (agy treats Stop-hook stdout as a
# result that can block the stop); avoids `set -e` so a malformed payload can't
# disrupt agy's loop.

# MNGR_AGENT_STATE_DIR is always set in the real hook path (agy invokes this via
# a path that embeds it), so an unset value is a wiring bug: fail loudly to
# stderr rather than silently mishandle the marker.
if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "clear_active_marker_when_idle.sh: MNGR_AGENT_STATE_DIR is not set" >&2
    exit 1
fi

marker_file="$MNGR_AGENT_STATE_DIR/active"

payload=$(cat)

# Match only the positive `"fullyIdle":true` form (tolerating JSON whitespace);
# leave the marker for `false`, an absent field, or garbage, so the agent stays
# RUNNING. POSIX grep only -- no jq (it may be absent on remote hosts).
if printf '%s' "$payload" | grep -qE '"fullyIdle"[[:space:]]*:[[:space:]]*true'; then
    rm -f "$marker_file"
fi
