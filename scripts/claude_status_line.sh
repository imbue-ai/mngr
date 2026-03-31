#!/usr/bin/env bash
set -euo pipefail
# Status line script for Claude Code
# Outputs: [time user@host dir] branch | PR: url (status)

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

# Get PR URL from .claude/pr_url (if exists)
PR_URL=""
if [[ -f "${MNGR_AGENT_WORK_DIR:-.}/.claude/pr_url" ]]; then
    PR_URL=$(cat "${MNGR_AGENT_WORK_DIR:-.}/.claude/pr_url" 2>/dev/null || echo "")
fi

# Get PR status from .claude/pr_status (if exists)
PR_STATUS=""
if [[ -f "${MNGR_AGENT_WORK_DIR:-.}/.claude/pr_status" ]]; then
    PR_STATUS=$(cat "${MNGR_AGENT_WORK_DIR:-.}/.claude/pr_status" 2>/dev/null || echo "")
fi

# Build the status line
STATUS_LINE="[$TIME $USER@$HOST $DIR]"

# Add branch info
if [[ -n "$BRANCH" ]]; then
    STATUS_LINE="$STATUS_LINE $BRANCH"
fi

# Add PR info if available
if [[ -n "$PR_URL" ]]; then
    if [[ -n "$PR_STATUS" ]]; then
        STATUS_LINE="$STATUS_LINE | PR: $PR_URL ($PR_STATUS)"
    else
        STATUS_LINE="$STATUS_LINE | PR: $PR_URL"
    fi
fi

printf '%s' "$STATUS_LINE"
