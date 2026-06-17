#!/usr/bin/env bash
# mngr_common_transcript_lib.sh -- Shared primitives for common-transcript converters.
#
# Sourced by per-agent common_transcript.sh converters (claude, antigravity,
# codex) and by the turn-end hooks that flush them (claude's
# wait_for_stop_hook.sh, antigravity's statusline.sh, codex's
# clear_active_marker.sh and subagent_stopped.sh). Centralizes the parts of
# common-transcript
# handling that are structurally identical regardless of the agent's native
# session schema:
#
#   - mngr_common_transcript_acquire_lock
#       Block until the per-agent convert lock is held, then return 0. Returns 1
#       if it could not be taken within MNGR_CONVERT_LOCK_TIMEOUT seconds
#       (default 30). The lock is a coarse mkdir-based mutex serializing the
#       converter's read-modify-write so the background 5s daemon and an
#       on-demand `--single-pass` flush never both append the same events into
#       permanent duplicates (event-id dedup only skips IDs already present at
#       read time). mkdir is atomic on POSIX filesystems, so it doubles as the
#       mutex; a lock left by a crashed pass is broken once it is older than a
#       minute. Same idiom as codex's codex_marker_lock.
#
#   - mngr_common_transcript_release_lock
#       Drop the lock (idempotent).
#
#   - mngr_common_transcript_flush [lock_timeout_seconds]
#       Run one synchronous `--single-pass` of the raw streamer then the
#       common-transcript converter, in pipeline order, so a turn-end / WAITING
#       signal can't outrun the converter. Best-effort: gated on each script
#       existing (the common transcript is opt-in) and `|| true` so a flush
#       failure can never strand the caller's turn-end signal. The converter's
#       lock keeps this pass from racing the background daemon.
#       The optional lock_timeout_seconds (default: MNGR_CONVERT_LOCK_TIMEOUT or
#       30) bounds how long each pass waits for the convert lock -- the only
#       potentially-slow step -- so a latency-sensitive caller (e.g. a
#       SIGTERM/SIGINT handler) can cap how long the flush blocks. Implemented
#       as a per-pass MNGR_CONVERT_LOCK_TIMEOUT rather than a `timeout(1)`
#       wrapper so it stays portable to macOS, which has no `timeout` binary.
#
# Requires MNGR_AGENT_STATE_DIR. The lock is per-agent (exactly one converter
# runs per agent), so a single lock dir under the state dir is sufficient.
# Nothing runs at source time and the env is only read inside the functions, so
# sourcing this lib can never fail -- even from a context that has not yet
# validated MNGR_AGENT_STATE_DIR.

_mngr_common_transcript_lock_dir() {
    printf '%s/.common_transcript_convert.lock' "${MNGR_AGENT_STATE_DIR}"
}

mngr_common_transcript_acquire_lock() {
    local lock_dir timeout waited
    lock_dir="$(_mngr_common_transcript_lock_dir)"
    timeout="${MNGR_CONVERT_LOCK_TIMEOUT:-30}"
    waited=0
    while ! mkdir "$lock_dir" 2>/dev/null; do
        if [ -n "$(find "$lock_dir" -maxdepth 0 -mmin +1 2>/dev/null)" ]; then
            rmdir "$lock_dir" 2>/dev/null || true
            continue
        fi
        if [ "$waited" -ge "$timeout" ]; then
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    return 0
}

mngr_common_transcript_release_lock() {
    rmdir "$(_mngr_common_transcript_lock_dir)" 2>/dev/null || true
}

mngr_common_transcript_flush() {
    local lock_timeout="${1:-${MNGR_CONVERT_LOCK_TIMEOUT:-30}}"
    local cmds="${MNGR_AGENT_STATE_DIR}/commands"
    if [ -x "$cmds/stream_transcript.sh" ]; then
        MNGR_CONVERT_LOCK_TIMEOUT="$lock_timeout" \
            bash "$cmds/stream_transcript.sh" --single-pass >/dev/null 2>&1 || true
    fi
    if [ -x "$cmds/common_transcript.sh" ]; then
        MNGR_CONVERT_LOCK_TIMEOUT="$lock_timeout" \
            bash "$cmds/common_transcript.sh" --single-pass >/dev/null 2>&1 || true
    fi
}
