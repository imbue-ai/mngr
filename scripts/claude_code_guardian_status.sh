#!/usr/bin/env bash
set -euo pipefail
# Outputs a one-line summary of the imbue-code-guardian gate states, e.g.:
#   code guardian: stop hook✓ autofix· conversation· architecture· ci·
# Each gate is shown with a colored glyph: green check (done/on), yellow
# ellipsis (pending), red cross (failed, CI only), dim dot (disabled).
# Prints nothing if no .reviewer/settings*.json is present.

# ANSI colors
GREEN=$'\033[32m'
YELLOW=$'\033[33m'
RED=$'\033[31m'
DIM=$'\033[90m'
RESET=$'\033[0m'

WORK_DIR="${MNGR_AGENT_WORK_DIR:-.}"
BASE_SETTINGS="$WORK_DIR/.reviewer/settings.json"
LOCAL_SETTINGS="$WORK_DIR/.reviewer/settings.local.json"

if [[ ! -f "$BASE_SETTINGS" && ! -f "$LOCAL_SETTINGS" ]]; then
    exit 0
fi

# Mirrors the precedence used by the imbue-code-guardian plugin's
# config_utils.sh: env var override (CODE_GUARDIAN_<KEY uppercased, dots
# -> __>) > settings.local.json > settings.json > default.
read_setting() {
    local key="$1"
    local default="$2"
    local jq_path=".$key"
    local val

    # Env-var override: matches read_json_config in the upstream plugin so
    # users who configure gates via env vars see consistent state here.
    local env_var
    env_var="CODE_GUARDIAN_$(echo "$key" | tr '[:lower:]' '[:upper:]' | sed 's/\./__/g')"
    if [[ -n "${!env_var:-}" ]]; then
        echo "${!env_var}"
        return
    fi

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

HASH=$(git rev-parse HEAD 2>/dev/null || echo "")
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
BRANCH_SANITIZED="${BRANCH//\//_}"

PR_STATUS=""
if [[ -f "$WORK_DIR/.reviewer/outputs/pr_status" ]]; then
    PR_STATUS=$(cat "$WORK_DIR/.reviewer/outputs/pr_status" 2>/dev/null || echo "")
fi

# stop_hook: master switch. Evaluate the shell expression in enabled_when.
STOP_EXPR=$(read_setting "stop_hook.enabled_when" "true")
if eval "$STOP_EXPR" >/dev/null 2>&1; then
    STOP_GATE=$(fmt_gate "stop hook" "$GREEN" "✓")
else
    STOP_GATE=$(fmt_gate "stop hook" "$DIM" "·")
fi

# autofix: per-commit completion file. Append "(major+critical)" when
# configured to ignore minor issues; detected via the unique marker phrase
# the reviewer-autofix-ignore-minor-issues skill writes into append_to_prompt.
AUTOFIX_ENABLED=$(read_setting "autofix.is_enabled" "true")
AUTOFIX_PROMPT=$(read_setting "autofix.append_to_prompt" "")
AUTOFIX_LABEL="autofix"
if [[ "$AUTOFIX_PROMPT" == *"MAJOR and CRITICAL"* ]]; then
    AUTOFIX_LABEL="autofix(major+critical)"
fi
if [[ "$AUTOFIX_ENABLED" != "true" ]]; then
    AUTOFIX_GATE=$(fmt_gate "$AUTOFIX_LABEL" "$DIM" "·")
elif [[ -n "$HASH" && -f "$WORK_DIR/.reviewer/outputs/autofix/${HASH}_verified.md" ]]; then
    AUTOFIX_GATE=$(fmt_gate "$AUTOFIX_LABEL" "$GREEN" "✓")
else
    AUTOFIX_GATE=$(fmt_gate "$AUTOFIX_LABEL" "$YELLOW" "⋯")
fi

# verify_conversation: per-commit completion file.
CONV_ENABLED=$(read_setting "verify_conversation.is_enabled" "true")
if [[ "$CONV_ENABLED" != "true" ]]; then
    CONV_GATE=$(fmt_gate "conversation" "$DIM" "·")
elif [[ -n "$HASH" && -f "$WORK_DIR/.reviewer/outputs/conversation/${HASH}.json" ]]; then
    CONV_GATE=$(fmt_gate "conversation" "$GREEN" "✓")
else
    CONV_GATE=$(fmt_gate "conversation" "$YELLOW" "⋯")
fi

# verify_architecture: per-branch completion file.
ARCH_ENABLED=$(read_setting "verify_architecture.is_enabled" "true")
if [[ "$ARCH_ENABLED" != "true" ]]; then
    ARCH_GATE=$(fmt_gate "architecture" "$DIM" "·")
elif [[ -n "$BRANCH_SANITIZED" && -f "$WORK_DIR/.reviewer/outputs/architecture/${BRANCH_SANITIZED}.md" ]]; then
    ARCH_GATE=$(fmt_gate "architecture" "$GREEN" "✓")
else
    ARCH_GATE=$(fmt_gate "architecture" "$YELLOW" "⋯")
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

printf 'code guardian: %s %s %s %s %s' \
    "$STOP_GATE" "$AUTOFIX_GATE" "$CONV_GATE" "$ARCH_GATE" "$CI_GATE"
