#!/usr/bin/env sh
# Shared lifecycle-marker state helpers for the codex hook scripts.
#
# Sourced by all four codex lifecycle hooks (set_active_marker.sh,
# clear_active_marker.sh, subagent_started.sh, subagent_stopped.sh). It owns the
# state-file path variables and the small set of POSIX-sh functions those hooks
# share, so the lock protocol and the marker-recompute invariant live in exactly
# one place.
#
# Why a recompute-under-lock model instead of a plain touch/remove on the marker:
# codex subagents (the spawn_agent multi-agent feature) run ASYNCHRONOUSLY. The
# root agent's Stop hook fires when the root model loop is done WHILE its
# subagents are still running; their SubagentStop hooks arrive later, with no
# ordering guarantee, and codex emits no fullyIdle-style signal. So the `active`
# marker (which core reads as RUNNING) must stay present until the root turn AND
# every in-flight subagent are done. We track those two facts as separate state
# (the `codex_root_active` flag and one file per live subagent under
# `codex_subagents/`) and recompute the marker from them on every event:
#
#   INVARIANT: `active` exists IFF (`codex_root_active` exists OR
#              `codex_subagents/` is non-empty).
#
# Because four hooks (and possibly several concurrent subagent hooks) mutate this
# state, every event takes a coarse mkdir-based lock, mutates, recomputes, and
# unlocks. mkdir is atomic on POSIX filesystems, so it doubles as the mutex.
#
# This file is meant to be sourced, so it never calls `exit` at the top level and
# never enables `set -e` -- a hook that sources it stays in control of its own
# exit. Each hook acquires the lock, mutates, recomputes, and unlocks explicitly;
# the stale-lock break below recovers a lock orphaned by a crashed hook.

# Require the agent state dir. Echo to stderr and return non-zero (never exit) so
# the sourcing hook can decide how to fail; a hook with no state dir is a wiring
# error that should surface loudly.
if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "codex_marker_state.sh: MNGR_AGENT_STATE_DIR is not set" >&2
    return 1
fi

# State-file paths (all under $MNGR_AGENT_STATE_DIR). Names are kept in sync with
# the corresponding constants in codex_config.py.
CODEX_MARKER_FILE="$MNGR_AGENT_STATE_DIR/active"
CODEX_ROOT_ACTIVE_FILE="$MNGR_AGENT_STATE_DIR/codex_root_active"
CODEX_SUBAGENTS_DIR="$MNGR_AGENT_STATE_DIR/codex_subagents"
CODEX_ROOT_SESSION_FILE="$MNGR_AGENT_STATE_DIR/codex_root_session"
CODEX_TRANSCRIPT_PATH_FILE="$MNGR_AGENT_STATE_DIR/codex_transcript_path"
CODEX_MARKER_LOCK_DIR="$MNGR_AGENT_STATE_DIR/codex_marker.lock"
# Permission-waiting flag (set by the PermissionRequest hook, cleared by
# PostToolUse). Independent of the active-marker recompute; clear_active_marker.sh
# removes it on the root Stop as a safety net against a stranded dialog.
CODEX_PERMISSIONS_WAITING_FILE="$MNGR_AGENT_STATE_DIR/permissions_waiting"

# Acquire the marker lock by atomically creating the lock dir, retrying briefly
# while another hook holds it. Hooks complete in well under a second, so a short
# retry loop (0.1s, capped near 60s) is generous. STALE-BREAK: a lock dir older
# than one minute can only mean a hook died holding it (no hook runs that long),
# so steal it via rmdir and retry. `find -mmin +1` is portable across Linux and
# macOS, unlike `stat`'s differing flags.
codex_marker_lock() {
    _codex_lock_attempts=0
    while ! mkdir "$CODEX_MARKER_LOCK_DIR" 2>/dev/null; do
        if [ -n "$(find "$CODEX_MARKER_LOCK_DIR" -maxdepth 0 -mmin +1 2>/dev/null)" ]; then
            rmdir "$CODEX_MARKER_LOCK_DIR" 2>/dev/null || true
            continue
        fi
        _codex_lock_attempts=$((_codex_lock_attempts + 1))
        if [ "$_codex_lock_attempts" -ge 600 ]; then
            # Give up waiting but proceed unlocked rather than dropping the event;
            # a stranded marker is worse than a rare unsynchronized recompute.
            echo "codex_marker_state.sh: gave up waiting for $CODEX_MARKER_LOCK_DIR" >&2
            return 0
        fi
        sleep 0.1
    done
    return 0
}

# Release the marker lock. Tolerates an already-absent lock dir (e.g. when the
# stale-break or the give-up path left us running without holding it).
codex_marker_unlock() {
    rmdir "$CODEX_MARKER_LOCK_DIR" 2>/dev/null || true
}

# Recompute the `active` marker from the tracked state, enforcing the invariant:
# the marker exists iff the root turn is active or at least one subagent is in
# flight.
codex_marker_recompute() {
    if [ -e "$CODEX_ROOT_ACTIVE_FILE" ] || [ -n "$(ls -A "$CODEX_SUBAGENTS_DIR" 2>/dev/null)" ]; then
        touch "$CODEX_MARKER_FILE"
    else
        rm -f "$CODEX_MARKER_FILE"
    fi
}

# Extract the first value of a `"<key>":"<value>"` JSON string field from a
# payload. Usage: codex_extract_field <key> <payload>. transcript_path may
# contain spaces and slashes, so the value is matched up to the first closing
# quote rather than constrained to a UUID shape. POSIX grep/sed only -- no jq
# (it may be absent on remote hosts).
codex_extract_field() {
    _codex_field_key="$1"
    _codex_field_payload="$2"
    printf '%s' "$_codex_field_payload" \
        | grep -oE "\"$_codex_field_key\":\"[^\"]*\"" \
        | head -n 1 \
        | sed -E "s/^\"$_codex_field_key\":\"(.*)\"\$/\1/"
}
