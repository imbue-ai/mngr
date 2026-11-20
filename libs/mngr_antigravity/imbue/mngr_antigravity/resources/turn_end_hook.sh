#!/usr/bin/env bash
# turn_end_hook.sh PreInvocation|Stop: records how the root conversation's turn ended in $MNGR_AGENT_STATE_DIR/turn_end.
# Prints nothing: agy reads PreInvocation stdout as injected steps and Stop stdout as a decision.

set -euo pipefail

if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "turn_end_hook.sh: MNGR_AGENT_STATE_DIR is not set" >&2
    exit 1
fi

event="${1:-}"
payload=$(cat)
turn_end_file="$MNGR_AGENT_STATE_DIR/turn_end"

# The value of the payload's first `"<key>":"<value>"`.
_payload_field() {
    printf '%s' "$payload" \
        | grep -oE "\"$1\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" \
        | head -n 1 \
        | sed -E 's/.*:[[:space:]]*"([^"]*)"$/\1/' \
        || true
}

# A subagent's payload names its own conversation, not the root that statusline.sh records.
conversation_id=$(_payload_field conversationId)
root_conversation=$(cat "$MNGR_AGENT_STATE_DIR/root_conversation" 2>/dev/null || true)
if [ -z "$conversation_id" ] || [ "$conversation_id" != "$root_conversation" ]; then
    exit 0
fi

# A root invocation means its turn is still open.
if [ "$event" = "PreInvocation" ]; then
    rm -f "$turn_end_file"
    exit 0
fi
[ "$event" = "Stop" ] || exit 0

termination_reason=$(_payload_field terminationReason)
case "$termination_reason" in
    NO_TOOL_CALL) kind=COMPLETED ;;
    ERROR) kind=FAILED ;;
    USER_CANCELED | MAX_INVOCATIONS | MAX_FORCED_INVOCATIONS | MAX_TOKEN_BUDGET_EXCEEDED | TERMINAL_STEP_TYPE | \
        TERMINAL_CUSTOM_HOOK | INJECTED_RESPONSE | EARLY_CONTINUE | CHAINED)
        kind=INTERRUPTED
        ;;
    *) kind="" ;;
esac

# agy ends a `/compact` with TERMINAL_CUSTOM_HOOK once the summary is written.
if [ "$termination_reason" = "TERMINAL_CUSTOM_HOOK" ]; then
    case "$(_payload_field lastUserInput)" in
        /compact | "/compact "*) kind="" ;;
    esac
fi

if [ -z "$kind" ]; then
    rm -f "$turn_end_file"
    exit 0
fi

if [ -r "$MNGR_AGENT_STATE_DIR/commands/mngr_background_task_lib.sh" ]; then
    # shellcheck source=../../../mngr/imbue/mngr/resources/mngr_background_task_lib.sh
    source "$MNGR_AGENT_STATE_DIR/commands/mngr_background_task_lib.sh"
fi
if command -v mngr_turn_end_write >/dev/null 2>&1; then
    mngr_turn_end_write "$kind" "$termination_reason"
fi
