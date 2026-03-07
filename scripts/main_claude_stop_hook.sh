#!/bin/bash
#
# main_claude_stop_hook.sh
#
# Orchestrator for stop hook scripts. Performs shared setup (precondition
# checks, fetch/merge/push, informational detection), then launches
# stop_hook_pr_and_ci.sh to handle PR creation and CI checks.

set -euo pipefail

# Read hook input JSON from stdin (must be done before anything else consumes stdin)
HOOK_INPUT=$(cat 2>/dev/null || echo '{}')

# Check if we're in a tmux session
if [ -z "${TMUX:-}" ]; then
    exit 0
fi

# Make sure we're the main claude session
if [ -z "${MAIN_CLAUDE_SESSION_ID:-}" ]; then
    # if not, this is some other claude session (not the main agent)
    exit 0
fi

# Use the latest session ID from the tracking file if available. Claude Code can
# replace its session (e.g., exit plan mode, /clear, compaction), creating a new
# session with a different UUID. The SessionStart hook writes the current session
# ID to $MNG_AGENT_STATE_DIR/claude_session_id so we can track it here.
if [ -n "${MNG_AGENT_STATE_DIR:-}" ] && [ -f "$MNG_AGENT_STATE_DIR/claude_session_id" ]; then
    _MNG_READ_SID=$(cat "$MNG_AGENT_STATE_DIR/claude_session_id")
    if [ -n "$_MNG_READ_SID" ]; then
        MAIN_CLAUDE_SESSION_ID="$_MNG_READ_SID"
    fi
fi

# Verify that all changes are committed (fail if not)
untracked=$(git ls-files --others --exclude-standard)
staged=$(git diff --cached --name-only)
unstaged=$(git diff --name-only)

if [ -n "$untracked" ] || [ -n "$staged" ] || [ -n "$unstaged" ]; then
    echo "ERROR: Uncommitted changes detected. All changes must be committed before this hook can run." >&2
    echo "ERROR: Please commit or gitignore all files before stopping." >&2
    exit 2
fi

# Get the current tmux session name
TMUX_SESSION=$(tmux display-message -p '#S' 2>/dev/null)
if [ -z "$TMUX_SESSION" ]; then
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Set up file logging before sourcing common
if [[ -n "${MNG_AGENT_STATE_DIR:-}" ]]; then
    mkdir -p "$MNG_AGENT_STATE_DIR/events/logs/stop_hook" 2>/dev/null || true
    export STOP_HOOK_LOG="$MNG_AGENT_STATE_DIR/events/logs/stop_hook/events.jsonl"
fi
export STOP_HOOK_SCRIPT_NAME="main_stop_hook"

# Source shared function definitions (log_error, log_warn, log_info, retry_command)
source "$SCRIPT_DIR/stop_hook_common.sh"

_log_to_file "INFO" "========================================================"
_log_to_file "INFO" "Stop hook started (pid=$$, ppid=$PPID)"
_log_to_file "INFO" "TMUX_SESSION=$TMUX_SESSION, MAIN_CLAUDE_SESSION_ID=$MAIN_CLAUDE_SESSION_ID"
_log_to_file "INFO" "========================================================"

# Trap signals so we can log unexpected terminations
_on_signal() {
    local sig="$1"
    _log_to_file "ERROR" "main_stop_hook received signal $sig (pid=$$) -- UNEXPECTED TERMINATION"
    exit 128
}
for _sig in HUP INT QUIT TERM PIPE; do
    trap "_on_signal $_sig" "$_sig"
done
trap '_log_to_file "INFO" "main_stop_hook EXIT trap fired (pid=$$, exit_code=$?)"' EXIT

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
BASE_BRANCH="${GIT_BASE_BRANCH:-main}"

# Fetch all remotes and merge base branch to stay up-to-date
log_info "Fetching all remotes..."
git fetch --all

# Only push the base branch if it doesn't already exist on the origin
if ! git rev-parse --verify "origin/$BASE_BRANCH" >/dev/null 2>&1; then
    log_info "Pushing base branch to origin (not yet present remotely)..."
    if ! retry_command 3 git push origin "$BASE_BRANCH"; then
        log_error "Failed to push base branch after retries. Perhaps changes were made locally by the linter? Or maybe you forgot to commit something?"
        notify_user || echo "No notify_user function defined, skipping."
        exit 2
    fi
fi

# Merge the base branch from origin (if it exists)
if git rev-parse --verify "origin/$BASE_BRANCH" >/dev/null 2>&1; then
    log_info "Merging origin/$BASE_BRANCH..."
    if ! git merge "origin/$BASE_BRANCH" --no-edit; then
        log_error "Merge conflict detected while merging origin/$BASE_BRANCH."
        log_error "Please resolve the merge conflicts before continuing."
        exit 2
    fi
fi

# Merge the local base branch (if it exists)
if git rev-parse --verify "$BASE_BRANCH" >/dev/null 2>&1; then
    log_info "Merging $BASE_BRANCH..."
    if ! git merge "$BASE_BRANCH" --no-edit; then
        log_error "Merge conflict detected while merging $BASE_BRANCH."
        log_error "Please resolve the merge conflicts before continuing."
        exit 2
    fi
fi

# Push merge commits (if any were created), setting upstream tracking if needed
log_info "Pushing any merge commits..."
if ! retry_command 3 git push -u origin HEAD; then
    log_error "Failed to push merge commits after retries. Perhaps you forgot to commit something? Or pre-commit hooks changed something? Or you made a mistake and modified a previous commit?"
    exit 2
fi

# Check if there are any non-markdown file changes compared to the base branch
IS_INFORMATIONAL_ONLY=false
if [[ "$CURRENT_BRANCH" == "$BASE_BRANCH" ]]; then
    log_info "Currently on base branch ($BASE_BRANCH) - no PR needed"
    IS_INFORMATIONAL_ONLY=true
else
    # Get files that have changed since the (now updated) base branch
    CHANGED_FILES=$(git diff --name-only "$BASE_BRANCH"...HEAD 2>/dev/null || echo "")
    if [[ -z "$CHANGED_FILES" ]]; then
        log_info "No files changed compared to $BASE_BRANCH - this was an informational session"
        IS_INFORMATIONAL_ONLY=true
    else
        # If all changed files are .md files, consider this an informational session
        NON_MD_FILES=$(echo "$CHANGED_FILES" | grep -v '\.md$' || true)
        if [[ -z "$NON_MD_FILES" ]]; then
            log_info "Only .md files changed compared to $BASE_BRANCH - this was an informational session"
            IS_INFORMATIONAL_ONLY=true
        fi
    fi
fi

if [[ "$IS_INFORMATIONAL_ONLY" == "true" ]]; then
    log_info "No code changes detected compared to $BASE_BRANCH - this is an informational session. Exiting cleanly."
    _log_to_file "INFO" "Informational-only session, exiting cleanly (exit 0)"
    notify_user || echo "No notify_user function defined, skipping."
    rm -f "$MNG_AGENT_STATE_DIR/active"
    exit 0
fi

# Export variables needed by child scripts
export TMUX_SESSION SCRIPT_DIR CURRENT_BRANCH BASE_BRANCH
export RED GREEN YELLOW NC

_log_to_file "INFO" "Launching PR/CI script..."

"$SCRIPT_DIR/stop_hook_pr_and_ci.sh"
PR_CI_EXIT=$?
_log_to_file "INFO" "PR/CI script exited with code $PR_CI_EXIT"

if [[ $PR_CI_EXIT -ne 0 ]]; then
    if [[ $PR_CI_EXIT -eq 2 ]]; then
        log_error "PR/CI hook failed (exit code 2)"
        log_error "Use the gh tool to inspect the remote test results for this branch and see what failed."
        log_error "Note that you MUST identify the issue and fix it locally before trying again!"
        log_error "NEVER just re-trigger the pipeline!"
        log_error "NEVER fix timeouts by increasing them! Instead, make things faster or increase parallelism."
        log_error "If it is impossible to fix the test, tell the user and say that you failed."
        log_error "Otherwise, once you have understood and fixed the issue, you can simply commit to try again."
    fi
    _log_to_file "ERROR" "main_stop_hook exiting with PR/CI exit code $PR_CI_EXIT"
    notify_user || echo "No notify_user function defined, skipping."
    exit $PR_CI_EXIT
fi

# Call local notification script if it exists
_log_to_file "INFO" "main_stop_hook completed successfully (exit 0)"
rm -f "$MNG_AGENT_STATE_DIR/active"
notify_user || echo "No notify_user function defined, skipping."

exit 0
