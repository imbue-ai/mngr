#!/usr/bin/env bash
#
# stop_hook_pr_and_ci.sh
#
# Checks for an existing PR and polls CI tests to completion.
# PR creation is the agent's responsibility (via CLAUDE.md instructions).
# Launched by main_claude_stop_hook.sh with TMUX_SESSION, SCRIPT_DIR,
# CURRENT_BRANCH, and BASE_BRANCH exported in the environment.

set -euo pipefail

STOP_HOOK_SCRIPT_NAME="pr_and_ci"
# Source shared function definitions (log_error, log_warn, log_info, retry_command)
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/stop_hook_common.sh"

_log_to_file "INFO" "pr_and_ci started (pid=$$, ppid=$PPID)"

EXISTING_PR=""

# Check if PR already exists (the agent is expected to create the PR itself)
log_info "Checking for existing PR..."
PR_STATE=""
if PR_INFO=$(gh pr view "$CURRENT_BRANCH" --json number,state 2>/dev/null); then
    EXISTING_PR=$(echo "$PR_INFO" | jq -r '.number')
    PR_STATE=$(echo "$PR_INFO" | jq -r '.state')
    log_info "Found existing PR #$EXISTING_PR (state: $PR_STATE)"
fi

if [[ -z "$EXISTING_PR" ]]; then
    if [[ "${MNGR_SKIP_STOP_HOOK_PR_CREATION:-0}" == "1" ]]; then
        log_info "MNGR_SKIP_STOP_HOOK_PR_CREATION=1 and no existing PR, skipping"
        _log_to_file "INFO" "Skipped (MNGR_SKIP_STOP_HOOK_PR_CREATION=1, no existing PR)"
    else
        log_error "No PR found for branch $CURRENT_BRANCH."
        log_error "Please create a draft PR using: gh pr create --draft"
        _log_to_file "ERROR" "No PR found for branch $CURRENT_BRANCH, exiting with error"
        exit 2
    fi
elif [[ "$PR_STATE" == "MERGED" ]]; then
    log_info "PR #$EXISTING_PR is already merged, skipping CI polling"
    _log_to_file "INFO" "PR #$EXISTING_PR already merged, skipping CI polling"
    EXISTING_PR=""
elif [[ "$PR_STATE" == "CLOSED" ]]; then
    # PR was closed but not merged - reopen it
    log_info "PR #$EXISTING_PR is closed. Reopening..."
    if gh pr reopen "$EXISTING_PR" --comment "Reopening PR for continued work."; then
        log_info "Reopened PR #$EXISTING_PR"
    else
        log_error "Failed to reopen PR #$EXISTING_PR"
        exit 1
    fi
fi

# Write PR URL to .claude/pr_url for status line display
if [[ -n "$EXISTING_PR" ]]; then
    PR_URL=$(gh pr view "$EXISTING_PR" --json url --jq '.url' 2>/dev/null || echo "")
    if [[ -n "$PR_URL" ]]; then
        echo "$PR_URL" > .claude/pr_url
        log_info "Wrote PR URL to .claude/pr_url: $PR_URL"
    fi
fi

# Initialize PR status as pending before polling
echo "pending" > .claude/pr_status

# Poll for PR checks to complete and report result
if [[ -n "$EXISTING_PR" ]]; then
    log_info "Polling for PR check results..."
    _log_to_file "INFO" "Starting poll_pr_checks.sh for PR #$EXISTING_PR"
    if RESULT=$("$SCRIPT_DIR/poll_pr_checks.sh" "$EXISTING_PR"); then
        echo "$RESULT"
        # Write successful status to .claude/pr_status
        echo "success" > .claude/pr_status
        log_info "Wrote PR status to .claude/pr_status: success"
        _log_to_file "INFO" "PR checks passed, exiting with 0"
    else
        POLL_EXIT=$?
        # Write failure status to .claude/pr_status
        echo "failure" > .claude/pr_status
        _log_to_file "ERROR" "poll_pr_checks.sh exited with code $POLL_EXIT"
        log_info "Wrote PR status to .claude/pr_status: failure"
        log_error "CI tests have failed for the PR!"
        log_error "Use the gh tool to inspect the remote test results for this branch and see what failed."
        log_error "Note that you MUST identify the issue and fix it locally before trying again!"
        log_error "NEVER just re-trigger the pipeline!"
        log_error "NEVER fix timeouts by increasing them! Instead, make things faster or increase parallelism."
        log_error "If it is impossible to fix the test, tell the user and say that you failed."
        log_error "Otherwise, once you have understood and fixed the issue, you can simply commit to try again."
        _log_to_file "INFO" "pr_and_ci exiting with code 2 (CI failure)"
        exit 2
    fi
fi

_log_to_file "INFO" "pr_and_ci exiting with code 0"
exit 0
