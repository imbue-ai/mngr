#!/usr/bin/env bash
# The launch_* variables are assigned by the sourced launch.env.
# shellcheck disable=SC2154
#
# grok_launch.sh -- start grok for an mngr agent.
#
# Usage: grok_launch.sh <grok command words...>
#
# The words are the agent's configured command, cli_args and agent_args. The script resumes the
# agent's session, forks a pending clone source, or starts the agent's own session id, appending
# mngr's flags and the session arguments after the words. If that first attempt exits non-zero
# before grok's SessionStart hook ran, it starts once more in a fresh session. A pending fork whose
# source session is missing fails the launch.
#
# Settings come from $MNGR_AGENT_STATE_DIR/plugin/grok/launch.env, rendered at provision.

set -euo pipefail

state_dir="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR is not set}"
plugin_dir="$state_dir/plugin/grok"

# shellcheck source=/dev/null
. "$plugin_dir/launch.env"

_MNGR_LOG_TYPE="grok_launch"
_MNGR_LOG_SOURCE="logs/grok_launch"
_MNGR_LOG_FILE="$state_dir/events/logs/grok_launch/events.jsonl"
# shellcheck source=/dev/null
source "$state_dir/commands/mngr_log.sh"

if [ "$#" -eq 0 ]; then
    echo "grok_launch.sh: no grok command given" >&2
    exit 2
fi

cd "$launch_work_dir"

# Nothing is in flight before grok starts, so every marker and lock a previous process left is stale.
rm -f "$state_dir/active" "$state_dir/permissions_waiting" "$state_dir/grok_session_started" \
    "$plugin_dir/background_tasks.json"
rm -rf "$plugin_dir/turns" "$plugin_dir/subagents" "$plugin_dir/pending_wake" \
    "$state_dir/.common_transcript_convert.lock"
touch "$state_dir/grok_process_started"

( env -u GROK_AGENT bash "$MNGR_AGENT_STATE_DIR/commands/grok_background_tasks.sh" "$(tmux display-message -p -t "$TMUX_PANE" '#{session_name}')" ) &

new_session_id() {
    python3 -I -c 'import uuid; print(uuid.uuid4())'
}

read_optional_file() {
    if [ -f "$1" ]; then
        cat "$1"
    fi
}

# A session is resumable once its directory holds summary.json; resuming any other id would
# trigger grok's slow remote restore.
is_session_resumable() {
    local session_dir
    for session_dir in "$launch_grok_home"/sessions/*/"$1"; do
        if [ -f "$session_dir/summary.json" ]; then
            return 0
        fi
    done
    return 1
}

run_grok() {
    if [ "$launch_is_auth_shared" = "true" ]; then
        set -- "GROK_AUTH_PATH=${GROK_AUTH_PATH:-$launch_user_auth_path}" "$@"
    fi
    set -- ${launch_pinned_environment[@]+"${launch_pinned_environment[@]}"} "$@"
    local removed_name
    for removed_name in ${launch_removed_environment[@]+"${launch_removed_environment[@]}"}; do
        set -- -u "$removed_name" "$@"
    done
    env "$@"
}

session_id="$(read_optional_file "$plugin_dir/session_id")"
if [ -z "$session_id" ]; then
    session_id="$(new_session_id)"
fi
fork_source_id="$(read_optional_file "$plugin_dir/fork_from")"

if is_session_resumable "$session_id"; then
    launch_mode="resume"
    rm -f "$plugin_dir/fork_from"
elif [ -n "$fork_source_id" ] && is_session_resumable "$fork_source_id"; then
    launch_mode="fork"
elif [ -n "$fork_source_id" ]; then
    log_error "Fork source session $fork_source_id is not in $launch_grok_home/sessions"
    echo "grok_launch.sh: the session to fork, $fork_source_id, is not in $launch_grok_home/sessions." >&2
    echo "Remove $plugin_dir/fork_from to start a fresh session instead." >&2
    exit 1
else
    launch_mode="new"
fi

if [ -n "$launch_model" ]; then
    set -- "$@" -m "$launch_model"
fi
if [ -n "$launch_reasoning_effort" ]; then
    set -- "$@" --reasoning-effort "$launch_reasoning_effort"
fi
role_prompt="$(read_optional_file "$plugin_dir/role_prompt.md")"
if [ -n "$role_prompt" ]; then
    set -- "$@" "--rules=$role_prompt"
fi

log_info "Launching grok ($launch_mode) with session $session_id${fork_source_id:+, fork source $fork_source_id}"
first_attempt_status=0
case "$launch_mode" in
    resume)
        run_grok "$@" --resume "$session_id" || first_attempt_status=$?
        ;;
    fork)
        run_grok "$@" --resume "$fork_source_id" --fork-session --session-id "$session_id" || first_attempt_status=$?
        ;;
    new)
        run_grok "$@" --session-id "$session_id" || first_attempt_status=$?
        ;;
    *)
        echo "grok_launch.sh: unknown launch mode $launch_mode" >&2
        exit 70
        ;;
esac

# A grok that reached SessionStart owns its session; a non-zero exit then is not a launch failure.
if [ "$first_attempt_status" -eq 0 ] || [ -e "$state_dir/grok_session_started" ]; then
    exit "$first_attempt_status"
fi

fresh_session_id="$(new_session_id)"
log_warn "grok ($launch_mode) exited with status $first_attempt_status before SessionStart; starting fresh session $fresh_session_id"
echo "grok exited with status $first_attempt_status before its session started; starting a fresh session" >&2
run_grok "$@" --session-id "$fresh_session_id"
