#!/usr/bin/env bash
#
# wait_for_stop_hook.sh
#
# A Claude Code Stop hook that waits for all other stop hooks to finish,
# then runs post-completion actions before marking the agent inactive.
#
# Phases:
#   1. Wait for all other stop hooks that were running at the start of the
#      grace period to exit (or for MAX_WAIT seconds to elapse).
#   2. Run post-completion actions:
#        - If the code-guardian orchestrator wrote
#          .reviewer/outputs/orchestrator_success, upload this commit's
#          autofix issue file
#          (.reviewer/outputs/autofix/issues/${HEAD_hash}.jsonl) to the
#          code-review-json Modal volume and remove the marker.
#        - Invoke notify_user (best-effort; silently skipped if the command
#          is not defined).
#   3. Mark the agent inactive and exit.
#
# Identification strategy:
#   All stop hooks and bash tool tasks are direct children of the Claude
#   process. They are distinguished by environment variables:
#     - Stop hooks: have CLAUDE_PROJECT_DIR in their environment
#     - Bash tool tasks: have CLAUDECODE=1 in their environment
#   We also skip node/claude internal processes.

set -euo pipefail

# --- Configuration (override via environment) ---
GRACE_PERIOD="${HOOK_GRACE_PERIOD:-3}"      # seconds before first check
POLL_INTERVAL="${HOOK_POLL_INTERVAL:-1}"    # seconds between polls
MAX_WAIT="${HOOK_MAX_WAIT:-120}"            # max seconds to wait for other hooks

# Lock-acquire timeout (seconds) handed to the transcript flush in mark_inactive.
# The flush's only potentially-slow step is waiting for the converter lock, so
# this bounds how long mark_inactive can block. The signal handler uses a short
# bound so SIGTERM/SIGINT exit promptly; every other path can afford the normal
# bound (which matches the converter's own default).
FLUSH_LOCK_TIMEOUT_NORMAL="${HOOK_FLUSH_LOCK_TIMEOUT:-30}"
FLUSH_LOCK_TIMEOUT_SIGNAL="${HOOK_FLUSH_LOCK_TIMEOUT_SIGNAL:-2}"

# Session guard: exit early if not a managed session
[ -z "${MAIN_CLAUDE_SESSION_ID:-}" ] && exit 0

# Drain stdin so we don't block Claude
cat > /dev/null 2>&1 || true

# --- Find the Claude ancestor process ---
find_claude_pid() {
    local pid=$$
    while [ "$pid" -gt 1 ] 2>/dev/null; do
        local comm
        comm=$(cat "/proc/$pid/comm" 2>/dev/null || echo "")
        if [ "$comm" = "claude" ]; then
            echo "$pid"
            return 0
        fi
        local next
        next=$(awk '/^PPid:/{print $2}' "/proc/$pid/status" 2>/dev/null || echo "")
        if [ -z "$next" ] || [ "$next" = "$pid" ]; then
            break
        fi
        pid=$next
    done
    return 1
}

# --- Identify our own wrapper (the direct child of Claude in our ancestry) ---
find_our_wrapper_pid() {
    local pid=$$
    local claude_pid=$1
    while [ "$pid" -gt 1 ] 2>/dev/null; do
        local ppid
        ppid=$(awk '/^PPid:/{print $2}' "/proc/$pid/status" 2>/dev/null || echo "")
        if [ "$ppid" = "$claude_pid" ]; then
            echo "$pid"
            return 0
        fi
        if [ -z "$ppid" ] || [ "$ppid" = "$pid" ]; then
            break
        fi
        pid=$ppid
    done
    echo "$PPID"
}

# --- Check if a process is a stop hook (has CLAUDE_PROJECT_DIR, not CLAUDECODE) ---
is_stop_hook() {
    local pid=$1
    # Must have CLAUDE_PROJECT_DIR
    if ! tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -q '^CLAUDE_PROJECT_DIR=' 2>/dev/null; then
        return 1
    fi
    # Must NOT have CLAUDECODE=1 (bash tool tasks)
    if tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -qx 'CLAUDECODE=1' 2>/dev/null; then
        return 1
    fi
    return 0
}

# --- Get list of other stop hook PIDs ---
get_other_stop_hooks() {
    local claude_pid=$1
    local our_wrapper=$2
    local result=()

    local children
    children=$(grep -l "^PPid:[[:space:]]*${claude_pid}$" /proc/[0-9]*/status 2>/dev/null | \
               sed 's|/proc/\([0-9]*\)/status|\1|' | sort -n || true)

    for child in $children; do
        [ -d "/proc/$child" ] || continue
        [ "$child" = "$our_wrapper" ] && continue
        is_stop_hook "$child" || continue
        result+=("$child")
    done

    echo "${result[*]}"
}

# --- Mark agent as inactive and emit activity event ---
# $1 (optional): reason for marking inactive (e.g. "signal:SIGTERM")
# $2 (optional): lock-acquire timeout (seconds) for the transcript flush
#                (default FLUSH_LOCK_TIMEOUT_NORMAL)
mark_inactive() {
    local reason="${1:-}"
    local flush_lock_timeout="${2:-$FLUSH_LOCK_TIMEOUT_NORMAL}"
    # Flush the transcript pipeline before clearing the marker (see the comment
    # above the lib source below). This lives in mark_inactive -- not
    # run_post_completion -- so it runs on EVERY turn-end path, including the
    # no-/proc fast path and the SIGTERM/SIGINT handler, both of which clear the
    # marker without going through run_post_completion. Best-effort and guarded
    # so a missing lib or flush failure can never strand the turn-end signal.
    # The lock-acquire timeout bounds how long the flush can block; the signal
    # handler passes a short value so interrupts exit promptly.
    if command -v mngr_common_transcript_flush >/dev/null 2>&1; then
        mngr_common_transcript_flush "$flush_lock_timeout"
    fi
    rm -f "$MNGR_AGENT_STATE_DIR/active" "$MNGR_AGENT_STATE_DIR/permissions_waiting"
    mkdir -p "$MNGR_HOST_DIR/events/mngr/activity"
    local extra=""
    if [ -n "$reason" ]; then
        extra=', "reason": "'"$reason"'"'
    fi
    echo '{"source": "mngr/activity", "type": "activity", "event_id": "evt-'"$(head -c 16 /dev/urandom | xxd -p)"'", "timestamp": "'"$(date -u +"%Y-%m-%dT%H:%M:%S.000000000Z")"'"'"$extra"'}' \
        >> "$MNGR_HOST_DIR/events/mngr/activity/events.jsonl"
}

# --- Post-completion: upload autofix issues to Modal volume (best-effort) ---
# Uploads only the current commit's issue file (autofix writes one file per
# commit at .reviewer/outputs/autofix/issues/{hash}.jsonl). Files from prior
# commits are ignored so that each commit's upload contains exactly its own
# issues.
upload_autofix_issues() {
    local commit
    commit=$(git rev-parse HEAD 2>/dev/null) || return
    [ -n "$commit" ] || return

    local issues_file=".reviewer/outputs/autofix/issues/${commit}.jsonl"
    if [ ! -s "$issues_file" ]; then
        return
    fi

    local nested_path="${commit:0:4}/${commit:4:4}/${commit:8:4}/${commit:12:4}/${commit:16}"
    local volume_name="code-review-json"
    local volume_mount="/code_reviews"

    # Method 1: Copy to mounted volume + sync (Modal sandbox)
    local mount_dir="${volume_mount}/${nested_path}"
    if mkdir -p "${mount_dir}" 2>/dev/null && cp "$issues_file" "${mount_dir}/autofix.json" 2>/dev/null; then
        sync "${volume_mount}" 2>/dev/null || true
    fi

    # Method 2: Upload via modal CLI (local machine with Modal credentials)
    uv run modal volume put "${volume_name}" "$issues_file" "/${nested_path}/autofix.json" --force 2>/dev/null || true
}

# --- Source the shared common-transcript helpers (provides the flush) ---
# The raw streamer (1s poll) and common-transcript converter (5s poll) lag
# behind Claude's session JSONL. mark_inactive clears the `active` marker, which
# is the turn-end signal `mngr wait --state WAITING` reads -- and consumers that
# harvest the final assistant message from the common transcript on that signal
# (e.g. Catalyst's mngr_runner) would otherwise race the converter.
# mngr_common_transcript_flush (defined in the shared lib) forces one synchronous
# pass of each converter, in pipeline order, so the common transcript reflects
# the final message before the marker is cleared (see the shared library header).
# mark_inactive calls it on every turn-end path. Sourced defensively: the lib is
# provisioned by Host._ensure_shared_shell_libs, but a missing lib must never
# strand the turn-end signal.
if [ -n "${MNGR_AGENT_STATE_DIR:-}" ] && [ -r "$MNGR_AGENT_STATE_DIR/commands/mngr_common_transcript_lib.sh" ]; then
    # shellcheck source=../../../mngr/imbue/mngr/resources/mngr_common_transcript_lib.sh
    source "$MNGR_AGENT_STATE_DIR/commands/mngr_common_transcript_lib.sh"
fi

# --- Post-completion actions (run after all other stop hooks finish) ---
run_post_completion() {
    # Only run post-completion if the orchestrator succeeded.
    # The code-guardian orchestrator writes .reviewer/outputs/orchestrator_success
    # on success with the commit hash.
    if [ -f ".reviewer/outputs/orchestrator_success" ]; then
        upload_autofix_issues
        rm -f ".reviewer/outputs/orchestrator_success"
    fi

    # Always notify the user (regardless of success/failure)
    notify_user 2>/dev/null || true
}

# --- Signal handler: mark inactive and exit on SIGTERM/SIGINT ---
on_signal() {
    local sig="$1"
    echo "wait_for_stop_hook: received SIG${sig}, marking inactive" >&2
    mark_inactive "signal:SIG${sig}" "$FLUSH_LOCK_TIMEOUT_SIGNAL"
    exit 0
}

trap 'on_signal TERM' TERM
trap 'on_signal INT' INT

# =====================================================================
# Main
# =====================================================================

CLAUDE_PID=$(find_claude_pid) || {
    echo "wait_for_stop_hook: could not find Claude ancestor process (no /proc?), marking inactive immediately" >&2
    mark_inactive
    exit 0
}

OUR_WRAPPER=$(find_our_wrapper_pid "$CLAUDE_PID")

echo "wait_for_stop_hook: Claude PID=$CLAUDE_PID, our wrapper=$OUR_WRAPPER, grace=${GRACE_PERIOD}s"

# Grace period: give Claude time to spawn all stop hooks
sleep "$GRACE_PERIOD"

# Snapshot the other stop hooks we need to wait for
INITIAL_HOOKS=$(get_other_stop_hooks "$CLAUDE_PID" "$OUR_WRAPPER")

if [ -z "$INITIAL_HOOKS" ]; then
    echo "wait_for_stop_hook: no other stop hooks found after grace period"
    run_post_completion
    mark_inactive
    exit 0
fi

echo "wait_for_stop_hook: waiting for stop hooks: $INITIAL_HOOKS (max ${MAX_WAIT}s)"

WAITED=0
while true; do
    ALL_DONE=true
    for hook_pid in $INITIAL_HOOKS; do
        if [ -d "/proc/$hook_pid" ]; then
            ALL_DONE=false
            break
        fi
    done

    if [ "$ALL_DONE" = true ]; then
        echo "wait_for_stop_hook: all other stop hooks have finished"
        run_post_completion
        mark_inactive
        exit 0
    fi

    if [ "$WAITED" -ge "$MAX_WAIT" ]; then
        echo "wait_for_stop_hook: timed out after ${MAX_WAIT}s waiting for hooks, marking inactive" >&2
        run_post_completion
        mark_inactive
        exit 0
    fi

    sleep "$POLL_INTERVAL"
    WAITED=$((WAITED + POLL_INTERVAL))
done
