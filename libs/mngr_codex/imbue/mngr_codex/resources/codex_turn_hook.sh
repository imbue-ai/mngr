#!/usr/bin/env bash
# codex hook that keeps the root thread's turn marker and dialog marker in $MNGR_AGENT_STATE_DIR.
#
# Usage: codex_turn_hook.sh <UserPromptSubmit|Stop|Interrupt|PermissionRequest|PreToolUse|PostToolUse>, codex's payload on stdin.
#
#   UserPromptSubmit   writes the payload's turn_id into `active` and removes `permissions_waiting`
#   Stop               rewrites the background task records with this turn counted as over, then removes
#                      `permissions_waiting`
#   Interrupt          rewrites the background task records with this turn counted as over, then removes
#                      `active` and `permissions_waiting`
#   PermissionRequest  touches `permissions_waiting`
#   PreToolUse         touches `permissions_waiting` when the tool is request_user_input
#   PostToolUse        removes `permissions_waiting`
#
# stream_transcript.sh removes `active` at the rollout's end of the turn, which codex writes after its last Stop.
#
# An event from a subagent thread (its payload has an agent_id), or from a session other than
# the root thread named in codex_app_server_thread, changes nothing. Fields are read only from
# the payload before its tool_input, which holds the tool's own JSON. Never writes stdout, since
# codex reads UserPromptSubmit stdout as model context. Exits 0 once MNGR_AGENT_STATE_DIR is set,
# even when a marker cannot be written or removed.

set -euo pipefail

if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "codex_turn_hook.sh: MNGR_AGENT_STATE_DIR is not set" >&2
    exit 1
fi

payload=$(cat) || :
payload=${payload%%\"tool_input\":*}

# Print the first `"<key>":"<value>"` string value in the payload.
_field() {
    printf '%s' "$payload" | { grep -oE "\"$1\":\"[^\"]*\"" || :; } | sed -n -E "1s/^\"$1\":\"(.*)\"\$/\1/p"
}

# Rewrite the background task records, counting the root thread's turn as over.
_record_background_tasks() {
    local writer="$MNGR_AGENT_STATE_DIR/commands/record_background_tasks.py"
    [ -f "$writer" ] && command -v python3 >/dev/null 2>&1 || return 0
    python3 "$writer" --turn-ended "$(_field session_id)" >/dev/null 2>&1 || :
}

case "$payload" in
    *'"agent_id":"'*) exit 0 ;;
esac

root_thread=$(cat "$MNGR_AGENT_STATE_DIR/codex_app_server_thread" 2>/dev/null || :)
if [ -n "$root_thread" ] && [ "$(_field session_id)" != "$root_thread" ]; then
    exit 0
fi

case "${1:-}" in
    UserPromptSubmit)
        printf '%s\n' "$(_field turn_id)" > "$MNGR_AGENT_STATE_DIR/active" || :
        rm -f "$MNGR_AGENT_STATE_DIR/permissions_waiting" || :
        ;;
    Stop)
        _record_background_tasks
        rm -f "$MNGR_AGENT_STATE_DIR/permissions_waiting" || :
        ;;
    Interrupt)
        _record_background_tasks
        rm -f "$MNGR_AGENT_STATE_DIR/active" "$MNGR_AGENT_STATE_DIR/permissions_waiting" || :
        ;;
    PermissionRequest)
        touch "$MNGR_AGENT_STATE_DIR/permissions_waiting" || :
        ;;
    PreToolUse)
        if [ "$(_field tool_name)" = "request_user_input" ]; then
            touch "$MNGR_AGENT_STATE_DIR/permissions_waiting" || :
        fi
        ;;
    PostToolUse)
        rm -f "$MNGR_AGENT_STATE_DIR/permissions_waiting" || :
        ;;
    *)
        echo "codex_turn_hook.sh: unknown event '${1:-}'" >&2
        ;;
esac
exit 0
