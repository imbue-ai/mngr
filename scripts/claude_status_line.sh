#!/usr/bin/env bash
set -euo pipefail
# Status line script for Claude Code
# Outputs: [time user@host dir] branch | PR: url (status) | Issues: N

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
if [[ -f .claude/pr_url ]]; then
    PR_URL=$(cat .claude/pr_url 2>/dev/null || echo "")
fi

# Get PR status from .claude/pr_status (if exists)
PR_STATUS=""
if [[ -f .claude/pr_status ]]; then
    PR_STATUS=$(cat .claude/pr_status 2>/dev/null || echo "")
fi

# Count serious issues from .reviews/final_issue_json/*.json
# Serious issues: severity is CRITICAL or MAJOR and confidence >= 0.7
# Note: Files are JSONL format (one JSON object per line), so use jq -s to slurp into array
ISSUES_COUNT=0
REVIEW_DIR=".reviews/final_issue_json"
if [[ -d "$REVIEW_DIR" ]]; then
    for json_file in "$REVIEW_DIR"/*.json; do
        if [[ -f "$json_file" ]]; then
            # Use jq -s to slurp JSONL lines into array, then count matching issues
            COUNT=$(jq -s '[.[] | select((.severity == "CRITICAL" or .severity == "MAJOR") and .confidence >= 0.7)] | length' "$json_file" 2>/dev/null || echo 0)
            ISSUES_COUNT=$((ISSUES_COUNT + COUNT))
        fi
    done
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

# Add issues count if there are serious issues
if [[ $ISSUES_COUNT -gt 0 ]]; then
    STATUS_LINE="$STATUS_LINE | Issues: $ISSUES_COUNT"
fi

printf '%s' "$STATUS_LINE"
