#!/usr/bin/env bash
set -euo pipefail
#
# create_reviewer_settings.sh
#
# Create or update .reviewer/settings.local.json with the given toggle values.
#
# Usage: create_reviewer_settings.sh [AUTOFIX_ENABLE] [CI_ENABLE] [VERIFY_CONVERSATION_ENABLE] [AUTOFIX_MINOR]
#
# Each argument is 1 (enabled) or 0 (disabled), defaulting to 1 if not set.
#   AUTOFIX_ENABLE            - autofix.is_enabled
#   CI_ENABLE                 - ci.is_enabled
#   VERIFY_CONVERSATION_ENABLE - verify_conversation.is_enabled
#   AUTOFIX_MINOR             - 1: fix all issues, 0: only MAJOR/CRITICAL

AUTOFIX_ENABLE="${1:-1}"
CI_ENABLE="${2:-1}"
VERIFY_CONVERSATION_ENABLE="${3:-1}"
AUTOFIX_MINOR="${4:-1}"

AUTOFIX_BOOL=$( [[ "$AUTOFIX_ENABLE" == "1" ]] && echo true || echo false )
CI_BOOL=$( [[ "$CI_ENABLE" == "1" ]] && echo true || echo false )
CONVO_BOOL=$( [[ "$VERIFY_CONVERSATION_ENABLE" == "1" ]] && echo true || echo false )

PROMPT_ALL='Please autofix as normal, except: Never ask questions. You are running unattended and the user is not there to answer your questions. Instead, think hard about whether to accept each given patch. If you decide *not* to accept it, then create a *new* branch with that fix commit. Call the branch (current_branch_name)___(fix_description) *and be sure to push it remotely* then by sure to check the normal branch back out when you'\''re done. Also be sure to tell the user that you did this.'

PROMPT_MAJOR='Please autofix as normal, except: 1. Never ask questions. You are running unattended and the user is not there to answer your questions. Instead, think hard about whether to accept each given patch. If you decide *not* to accept it, then create a *new* branch with that fix commit. Call the branch (current_branch_name)___(fix_description) *and be sure to push it remotely* then by sure to check the normal branch back out when you'\''re done. Also be sure to tell the user that you did this.  2. You only *have* to fix MAJOR and CRITICAL issues. If there are issues that you do NOT fix, append the json object(s) for those error(s) that were *not* fixed into ~/temp/issues/<current-git-hash>.jsonl  If so, be sure to mention those issues in your final summary as well.'

if [[ "$AUTOFIX_MINOR" == "1" ]]; then
    PROMPT="$PROMPT_ALL"
else
    PROMPT="$PROMPT_MAJOR"
fi

SETTINGS=".reviewer/settings.local.json"

jq -n \
    --argjson existing "$(cat "$SETTINGS" 2>/dev/null || echo '{}')" \
    --argjson autofix_enabled "$AUTOFIX_BOOL" \
    --argjson ci_enabled "$CI_BOOL" \
    --argjson convo_enabled "$CONVO_BOOL" \
    --arg prompt "$PROMPT" \
    '$existing * {
        "autofix": {"is_enabled": $autofix_enabled, "append_to_prompt": $prompt},
        "verify_conversation": {"is_enabled": $convo_enabled},
        "ci": {"is_enabled": $ci_enabled}
    }' > "${SETTINGS}.tmp" && mv "${SETTINGS}.tmp" "$SETTINGS"
