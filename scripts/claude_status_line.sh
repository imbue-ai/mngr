#!/usr/bin/env bash
set -euo pipefail
# Status line script for Claude Code
# Outputs two lines:
#   1. [time user@host dir] branch | PR: url (status)
#   2. reviewer: stop<g> autofix<g> conv<g> arch<g> ci<g>
# where each <g> is a colored glyph: green check (done/on),
# yellow ellipsis (pending), red cross (failed), dim dot (off).

# ANSI colors
GREEN=$'\033[32m'
YELLOW=$'\033[33m'
RED=$'\033[31m'
DIM=$'\033[90m'
RESET=$'\033[0m'

# Get basic info
TIME=$(date +%H:%M:%S)
USER=$(whoami)
HOST=$(hostname -s)
DIR=$(pwd)

# Get current git branch
BRANCH=""
if git rev-parse --git-dir > /dev/null 2>&1; then
    BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
fi

WORK_DIR="${MNGR_AGENT_WORK_DIR:-.}"

# Get PR URL from .reviewer/outputs/pr_url (if exists)
PR_URL=""
if [[ -f "$WORK_DIR/.reviewer/outputs/pr_url" ]]; then
    PR_URL=$(cat "$WORK_DIR/.reviewer/outputs/pr_url" 2>/dev/null || echo "")
fi

# Get PR status from .reviewer/outputs/pr_status (if exists)
PR_STATUS=""
if [[ -f "$WORK_DIR/.reviewer/outputs/pr_status" ]]; then
    PR_STATUS=$(cat "$WORK_DIR/.reviewer/outputs/pr_status" 2>/dev/null || echo "")
fi

# Build the first status line
LINE1="[$TIME $USER@$HOST $DIR]"

if [[ -n "$BRANCH" ]]; then
    LINE1="$LINE1 $BRANCH"
fi

if [[ -n "$PR_URL" ]]; then
    if [[ -n "$PR_STATUS" ]]; then
        LINE1="$LINE1 | PR: $PR_URL ($PR_STATUS)"
    else
        LINE1="$LINE1 | PR: $PR_URL"
    fi
fi

# --- Reviewer gates line ---
# Mirrors the precedence used by the imbue-code-guardian plugin's
# config_utils.sh: settings.local.json overrides settings.json.
BASE_SETTINGS="$WORK_DIR/.reviewer/settings.json"
LOCAL_SETTINGS="$WORK_DIR/.reviewer/settings.local.json"

read_setting() {
    local key="$1"
    local default="$2"
    local jq_path=".$key"
    local val
    if [[ -f "$LOCAL_SETTINGS" ]]; then
        val=$(jq -r "if $jq_path == null then empty else $jq_path end" "$LOCAL_SETTINGS" 2>/dev/null || true)
        if [[ -n "$val" ]]; then
            echo "$val"
            return
        fi
    fi
    if [[ -f "$BASE_SETTINGS" ]]; then
        val=$(jq -r "if $jq_path == null then empty else $jq_path end" "$BASE_SETTINGS" 2>/dev/null || true)
        if [[ -n "$val" ]]; then
            echo "$val"
            return
        fi
    fi
    echo "$default"
}

fmt_gate() {
    local label="$1"
    local color="$2"
    local glyph="$3"
    printf '%s%s%s%s' "$color" "$label" "$glyph" "$RESET"
}

LINE2=""
if [[ -f "$BASE_SETTINGS" || -f "$LOCAL_SETTINGS" ]]; then
    HASH=$(git rev-parse HEAD 2>/dev/null || echo "")
    BRANCH_SANITIZED="${BRANCH//\//_}"

    # stop_hook: master switch. Evaluate the shell expression in enabled_when.
    STOP_EXPR=$(read_setting "stop_hook.enabled_when" "true")
    if eval "$STOP_EXPR" >/dev/null 2>&1; then
        STOP_GATE=$(fmt_gate "stop" "$GREEN" "✓")
    else
        STOP_GATE=$(fmt_gate "stop" "$DIM" "·")
    fi

    # autofix: per-commit completion file.
    AUTOFIX_ENABLED=$(read_setting "autofix.is_enabled" "true")
    if [[ "$AUTOFIX_ENABLED" != "true" ]]; then
        AUTOFIX_GATE=$(fmt_gate "autofix" "$DIM" "·")
    elif [[ -n "$HASH" && -f "$WORK_DIR/.reviewer/outputs/autofix/${HASH}_verified.md" ]]; then
        AUTOFIX_GATE=$(fmt_gate "autofix" "$GREEN" "✓")
    else
        AUTOFIX_GATE=$(fmt_gate "autofix" "$YELLOW" "⋯")
    fi

    # verify_conversation: per-commit completion file.
    CONV_ENABLED=$(read_setting "verify_conversation.is_enabled" "true")
    if [[ "$CONV_ENABLED" != "true" ]]; then
        CONV_GATE=$(fmt_gate "conv" "$DIM" "·")
    elif [[ -n "$HASH" && -f "$WORK_DIR/.reviewer/outputs/conversation/${HASH}.json" ]]; then
        CONV_GATE=$(fmt_gate "conv" "$GREEN" "✓")
    else
        CONV_GATE=$(fmt_gate "conv" "$YELLOW" "⋯")
    fi

    # verify_architecture: per-branch completion file.
    ARCH_ENABLED=$(read_setting "verify_architecture.is_enabled" "true")
    if [[ "$ARCH_ENABLED" != "true" ]]; then
        ARCH_GATE=$(fmt_gate "arch" "$DIM" "·")
    elif [[ -n "$BRANCH_SANITIZED" && -f "$WORK_DIR/.reviewer/outputs/architecture/${BRANCH_SANITIZED}.md" ]]; then
        ARCH_GATE=$(fmt_gate "arch" "$GREEN" "✓")
    else
        ARCH_GATE=$(fmt_gate "arch" "$YELLOW" "⋯")
    fi

    # ci: state lives in .reviewer/outputs/pr_status.
    CI_ENABLED=$(read_setting "ci.is_enabled" "true")
    if [[ "$CI_ENABLED" != "true" ]]; then
        CI_GATE=$(fmt_gate "ci" "$DIM" "·")
    else
        case "$PR_STATUS" in
            success) CI_GATE=$(fmt_gate "ci" "$GREEN" "✓") ;;
            failure) CI_GATE=$(fmt_gate "ci" "$RED" "✗") ;;
            *)       CI_GATE=$(fmt_gate "ci" "$YELLOW" "⋯") ;;
        esac
    fi

    LINE2="reviewer: $STOP_GATE $AUTOFIX_GATE $CONV_GATE $ARCH_GATE $CI_GATE"
fi

if [[ -n "$LINE2" ]]; then
    printf '%s\n%s' "$LINE1" "$LINE2"
else
    printf '%s' "$LINE1"
fi
