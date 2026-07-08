#!/usr/bin/env bash
# Stop hook: mark the ROOT turn done, then recompute the `active` marker.
#
# codex runs this when the root model loop ends, passing a JSON payload on stdin
# with the session id (verified live: it also carries last_assistant_message,
# stop_hook_active, etc.). It clears the `codex_root_active` flag and recomputes
# the `active` marker -- but only for the root session recorded by
# set_active_marker.sh in `codex_root_session`. On the root's Stop it also clears
# any stranded `permissions_waiting` marker as a safety net (see below).
#
# Async-subagent model: codex subagents run ASYNCHRONOUSLY, so the root's Stop
# fires (root model loop done) WHILE its subagents may still be running; their
# SubagentStop hooks arrive later with no ordering guarantee, and codex emits no
# fullyIdle-style signal. So this hook must NOT unconditionally clear `active`.
# Instead it clears only the root-turn flag (`codex_root_active`) and lets the
# shared recompute keep `active` present while any per-subagent file under
# `codex_subagents/` remains. The marker therefore flips to WAITING only once the
# root turn is done AND no subagents are in flight -- whichever event (this Stop
# or the last SubagentStop) happens last performs the actual clear. See
# codex_marker_state.sh for the invariant and the lock.
#
# Nested-codex session guard: the only thing to defend against here is a
# *separate* nested/recursive codex process that shares this CODEX_HOME (and thus
# these hooks) and whose Stop carries a different session id. The clear of the
# root-turn flag is gated on the recorded root session: act only when the
# payload's session id matches the recorded root (the root's own Stop). A Stop
# carrying any other session id (a nested codex) returns without touching state,
# so the root keeps reporting RUNNING. As a liveness fallback, if no root session
# was recorded (empty or absent file), the flag is cleared anyway, so a failure
# to record the root can never strand the agent in RUNNING forever.
#
# Marker / root-file names are kept in sync with codex_config.py via the sourced
# helper. Never writes stdout (codex can treat Stop-hook stdout as a result that
# blocks the stop); avoids `set -e` so a malformed payload can't disrupt codex's
# loop.

if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "clear_active_marker.sh: MNGR_AGENT_STATE_DIR is not set" >&2
    exit 1
fi

payload=$(cat)

# shellcheck source=codex_marker_state.sh
. "$MNGR_AGENT_STATE_DIR/commands/codex_marker_state.sh"

# Extract this Stop's session id (a lowercase 8-4-4-4-12 hex UUID; an empty
# result just means none was present).
session_id=$(codex_extract_field "session_id" "$payload")

codex_marker_lock

# The root session recorded at the turn boundary (may be empty/absent).
root_session=""
if [ -f "$CODEX_ROOT_SESSION_FILE" ]; then
    root_session=$(cat "$CODEX_ROOT_SESSION_FILE" 2>/dev/null)
fi

# A Stop from a *different* session is a nested codex: leave all state untouched
# so the root keeps reporting RUNNING.
if [ -n "$root_session" ] && [ "$session_id" != "$root_session" ]; then
    codex_marker_unlock
    exit 0
fi

# This is the root's Stop (or no root was recorded -- the liveness fallback).
# Clear the root-turn flag and recompute; in-flight subagents keep the marker.
rm -f "$CODEX_ROOT_ACTIVE_FILE"
codex_marker_recompute

# Safety net: clear any stranded permission-waiting marker at turn end. Normally
# PostToolUse clears it once the approved tool runs, but a dialog that was
# cancelled/denied (or never resolved) before the turn ended would otherwise leave
# the agent reporting PERMISSIONS-WAITING forever. Independent of the active-marker
# recompute, so a simple remove is enough.
rm -f "$CODEX_PERMISSIONS_WAITING_FILE"

codex_marker_unlock

# Turn-end flush: if this recompute left the agent WAITING (the `active` marker
# is gone, so the root turn is done and no subagents are in flight), force one
# synchronous common-transcript pass. A consumer harvesting the final message on
# the WAITING signal would otherwise race the 5s converter daemon. Mirrors
# claude's wait_for_stop_hook.sh and agy's statusline.sh. Done after releasing
# the marker lock and gated defensively: the lib is provisioned by
# Host._ensure_shared_shell_libs, but a missing lib or flush failure must never
# disrupt codex's loop. mngr_common_transcript_flush writes nothing to stdout.
if [ ! -e "$CODEX_MARKER_FILE" ] && [ -r "$MNGR_AGENT_STATE_DIR/commands/mngr_common_transcript_lib.sh" ]; then
    # shellcheck source=../../../mngr/imbue/mngr/resources/mngr_common_transcript_lib.sh
    . "$MNGR_AGENT_STATE_DIR/commands/mngr_common_transcript_lib.sh"
    if command -v mngr_common_transcript_flush >/dev/null 2>&1; then
        mngr_common_transcript_flush
    fi
fi
