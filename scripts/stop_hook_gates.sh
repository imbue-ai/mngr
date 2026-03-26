#!/usr/bin/env bash
set -euo pipefail
#
# stop_hook_gates.sh
#
# Check whether autofix verification and conversation review have been
# completed. Exits 0 if all enabled gates pass, 2 if any are missing.
#
# Usage:
#   ./stop_hook_gates.sh [COMMIT_HASH]
#
# If COMMIT_HASH is omitted, uses the current HEAD.
#
# This script is used by:
#   - main_claude_stop_hook.sh (the full mng stop hook orchestrator)
#   - The mng-skills Claude Code plugin (as a standalone Stop hook)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config_utils.sh
source "$SCRIPT_DIR/config_utils.sh"

HASH="${1:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"

REVIEWER_SETTINGS=".reviewer/settings.json"

AUTOFIX_ENABLED=$(read_json_config "$REVIEWER_SETTINGS" "autofix.is_enabled" "true")
CONVO_ENABLED=$(read_json_config "$REVIEWER_SETTINGS" "verify_conversation.is_enabled" "true")

AUTOFIX_NEEDED=false
CONVO_NEEDED=false

if [[ "$AUTOFIX_ENABLED" == "true" ]] && [[ ! -f ".reviewer/outputs/autofix/verified.md" ]]; then
    AUTOFIX_NEEDED=true
fi

if [[ "$CONVO_ENABLED" == "true" ]] && [[ ! -f ".reviewer/outputs/conversation/${HASH}.json" ]]; then
    CONVO_NEEDED=true
fi

AUTOFIX_EXTRA_ARGS=$(read_json_config "$REVIEWER_SETTINGS" "autofix.append_to_prompt" "")
if [[ -n "$AUTOFIX_EXTRA_ARGS" ]]; then
    AUTOFIX_CMD="/autofix ${AUTOFIX_EXTRA_ARGS}"
else
    AUTOFIX_CMD="/autofix"
fi

if [[ "$AUTOFIX_NEEDED" == "true" && "$CONVO_NEEDED" == "true" ]]; then
    echo "Neither autofix nor conversation review have been run." >&2
    echo "Run /verify-conversation up to and including Step 4 (Spawn Agent). Spawn that agent in the background, then run \"${AUTOFIX_CMD}\" to verify your changes. Be sure to report the results from the background agent before returning control to the user (be sure to wait for it to finish if it hasn't yet and if you're done running autofix)." >&2
    exit 2
elif [[ "$AUTOFIX_NEEDED" == "true" ]]; then
    echo "Autofix has not been run yet. Run \"${AUTOFIX_CMD}\" to verify your changes." >&2
    exit 2
elif [[ "$CONVO_NEEDED" == "true" ]]; then
    echo "Conversation has not been reviewed. Run /verify-conversation before finishing." >&2
    exit 2
fi

exit 0
