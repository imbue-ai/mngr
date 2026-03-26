#!/usr/bin/env bash
set -euo pipefail
#
# stop_hook_gates.sh
#
# Check whether autofix verification, architecture verification, and
# conversation review have been completed. Exits 0 if all enabled gates
# pass, 2 if any are missing.
#
# Safety hatch: after 3 consecutive blocks at the same state, exits 0
# with a warning instead of blocking forever. This prevents infinite
# loops when the agent cannot make progress (e.g., waiting for user
# input).
#
# Usage:
#   ./stop_hook_gates.sh [COMMIT_HASH]
#
# If COMMIT_HASH is omitted, uses the current HEAD.
#
# This script is used by:
#   - main_claude_stop_hook.sh (the full mng stop hook orchestrator)
#   - The mng-code-review Claude Code plugin (as a standalone Stop hook)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config_utils.sh
source "$SCRIPT_DIR/config_utils.sh"

HASH="${1:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"

REVIEWER_SETTINGS=".reviewer/settings.json"

# ---------------------------------------------------------------------------
# Safety hatch: prevent infinite stop-hook loops.
#
# Track consecutive blocks in .reviewer/.stop_hook_consecutive_blocks.
# Each line is a commit hash from a blocked attempt. If the last 3
# entries are all the same hash, the agent is stuck -- let it through.
# ---------------------------------------------------------------------------
MAX_CONSECUTIVE_BLOCKS=3
BLOCK_TRACKER=".reviewer/.stop_hook_consecutive_blocks"

_check_stuck() {
    if [[ ! -f "$BLOCK_TRACKER" ]]; then
        return 1
    fi
    local last_n entry_count unique_count
    last_n=$(tail -n "$MAX_CONSECUTIVE_BLOCKS" "$BLOCK_TRACKER")
    entry_count=$(echo "$last_n" | wc -l | tr -d ' ')
    if [[ $entry_count -ge $MAX_CONSECUTIVE_BLOCKS ]]; then
        unique_count=$(echo "$last_n" | sort -u | wc -l | tr -d ' ')
        if [[ $unique_count -eq 1 ]]; then
            return 0
        fi
    fi
    return 1
}

if _check_stuck; then
    echo "SAFETY HATCH: Stop hook has blocked ${MAX_CONSECUTIVE_BLOCKS} consecutive times at the same commit ($HASH)." >&2
    echo "Letting the agent through to prevent an infinite loop." >&2
    echo "The review gates are still unsatisfied -- please investigate manually." >&2
    rm -f "$BLOCK_TRACKER"
    exit 0
fi

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
BRANCH_SANITIZED="${BRANCH//\//_}"
if [[ "$ARCH_ENABLED" == "true" ]] && [[ ! -f ".reviewer/outputs/architecture/${BRANCH_SANITIZED}.md" ]]; then
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
    # All gates passed -- clear the block tracker
    rm -f "$BLOCK_TRACKER"
    exit 0
fi

# Record this blocked attempt for stuck detection
mkdir -p "$(dirname "$BLOCK_TRACKER")" 2>/dev/null || true
echo "$HASH" >> "$BLOCK_TRACKER" 2>/dev/null || true

echo "The following review gates have not been satisfied:" >&2
for item in "${MISSING[@]}"; do
    echo "  - ${item}" >&2
done
echo "" >&2
if [[ ${#MISSING[@]} -gt 1 ]]; then
    echo "Run these before finishing. If possible, run /verify-conversation in the background while running the others." >&2
fi
exit 2
