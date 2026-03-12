#!/usr/bin/env bash
#
# stop_hook_reviewer.sh
#
# Ensures all reviewers have not found any major or critical issues.
# Launched by main_claude_stop_hook.sh with TMUX_SESSION, SCRIPT_DIR,
# CURRENT_BRANCH, and BASE_BRANCH exported in the environment.

set -euo pipefail

STOP_HOOK_SCRIPT_NAME="reviewer_hook"
# Source shared function definitions (log_error, log_warn, log_info, retry_command)
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/stop_hook_common.sh"

_log_to_file "INFO" "reviewer_hook started (pid=$$, ppid=$PPID)"

# Find all windows named reviewer_* and start review processes as background jobs
REVIEWER_PIDS=()
for window in $(tmux list-windows -t "$TMUX_SESSION" -F '#W' 2>/dev/null | grep '^reviewer_' || true); do
    "$SCRIPT_DIR/run_reviewer.sh" "$TMUX_SESSION" "$window" &
    REVIEWER_PIDS+=($!)
    _log_to_file "INFO" "Launched run_reviewer.sh for window=$window (pid=${REVIEWER_PIDS[-1]})"
done

if [[ ${#REVIEWER_PIDS[@]} -eq 0 ]]; then
    log_info "No reviewer windows found, skipping review"
    _log_to_file "INFO" "No reviewer windows found, exiting with 0"
    exit 0
fi

# Wait for all reviewer background jobs to complete
log_info "Waiting for ${#REVIEWER_PIDS[@]} reviewer(s) to complete..."
REVIEWER_FAILED=false

for pid in "${REVIEWER_PIDS[@]}"; do
    _log_to_file "INFO" "Waiting for reviewer pid=$pid..."
    wait "$pid" && EXIT_CODE=0 || EXIT_CODE=$?
    _log_to_file "INFO" "Reviewer pid=$pid exited with code $EXIT_CODE"
    if [[ $EXIT_CODE -ne 0 ]]; then
        if [[ $EXIT_CODE -eq 2 ]]; then
            # Exit code 2 means the reviewer found blocking issues (CRITICAL/MAJOR with confidence >= 0.7)
            # This is expected behavior - we'll surface it to the user after all reviewers complete
            log_warn "Reviewer process $pid found blocking issues (exit code 2)"
            REVIEWER_FAILED=true
        else
            # Other exit codes indicate internal errors that should be surfaced immediately
            log_error "Reviewer process $pid failed with internal error (exit code $EXIT_CODE)"
            log_error "This indicates a problem with the review infrastructure, not code issues."
            log_error "Exit code 3 = timeout waiting for review"
            _log_to_file "ERROR" "reviewer_hook exiting with code $EXIT_CODE (internal error)"
            exit $EXIT_CODE
        fi
    fi
done

if [[ "$REVIEWER_FAILED" == true ]]; then
    log_error "Some issues were identified by the review agent!"
    log_error "Run 'cat .reviews/final_issue_json/*.json' to see the issues."
    log_error "You MUST fix any CRITICAL or MAJOR issues (with confidence >= 0.7) before trying again."
    _log_to_file "INFO" "reviewer_hook exiting with code 2 (blocking issues found)"
    exit 2
else
    log_info "All reviewers completed successfully"
fi

_log_to_file "INFO" "reviewer_hook exiting with code 0"
exit 0
