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
ARCH_ENABLED=$(read_json_config "$REVIEWER_SETTINGS" "verify_architecture.is_enabled" "true")

AUTOFIX_NEEDED=false
CONVO_NEEDED=false
ARCH_NEEDED=false

if [[ "$AUTOFIX_ENABLED" == "true" ]] && [[ ! -f ".reviewer/outputs/autofix/verified.md" ]]; then
    AUTOFIX_NEEDED=true
fi

if [[ "$CONVO_ENABLED" == "true" ]] && [[ ! -f ".reviewer/outputs/conversation/${HASH}.json" ]]; then
    CONVO_NEEDED=true
fi

BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
if [[ "$ARCH_ENABLED" == "true" ]] && [[ ! -f ".reviewer/outputs/architecture/${BRANCH}.md" ]]; then
    ARCH_NEEDED=true
fi

AUTOFIX_EXTRA_ARGS=$(read_json_config "$REVIEWER_SETTINGS" "autofix.append_to_prompt" "")
if [[ -n "$AUTOFIX_EXTRA_ARGS" ]]; then
    AUTOFIX_CMD="/autofix ${AUTOFIX_EXTRA_ARGS}"
else
    AUTOFIX_CMD="/autofix"
fi

MISSING=()
if [[ "$ARCH_NEEDED" == "true" ]]; then
    MISSING+=("architecture verification (/verify-architecture)")
fi
if [[ "$AUTOFIX_NEEDED" == "true" ]]; then
    MISSING+=("autofix (${AUTOFIX_CMD})")
fi
if [[ "$CONVO_NEEDED" == "true" ]]; then
    MISSING+=("conversation review (/verify-conversation)")
fi

if [[ ${#MISSING[@]} -eq 0 ]]; then
    exit 0
fi

echo "The following review gates have not been satisfied:" >&2
for item in "${MISSING[@]}"; do
    echo "  - ${item}" >&2
done
echo "" >&2
if [[ ${#MISSING[@]} -gt 1 ]]; then
    echo "Run these before finishing. If possible, run /verify-conversation in the background while running the others." >&2
fi
exit 2
