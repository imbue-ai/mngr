#!/bin/bash
#
# main_claude_stop_hook.sh
#
# Orchestrator for stop hook scripts. Performs shared setup (precondition
# checks, fetch/merge/push, informational detection, stuck-agent tracking),
# then launches stop_hook_pr_and_ci.sh and stop_hook_reviewer.sh in parallel.

set -euo pipefail

# Read hook input JSON from stdin (must be done before anything else consumes stdin)
HOOK_INPUT=$(cat 2>/dev/null || echo '{}')

# Check if we're in a tmux session
if [ -z "${TMUX:-}" ]; then
    exit 0
fi

# Make sure we're the main claude session
if [ -z "${MAIN_CLAUDE_SESSION_ID:-}" ]; then
    # if not, this is a reviewer or some other random claude
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

# make the session id accessible to the reviewers
mkdir -p .claude
echo "$MAIN_CLAUDE_SESSION_ID" > .claude/sessionid

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
        log_error "Failed to push base branch after retries"
        notify_user || echo "No notify_user function defined, skipping."
        exit 1
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

# Push merge commits (if any were created)
log_info "Pushing any merge commits..."
if ! retry_command 3 git push origin HEAD; then
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

# Track the commit hash to detect stuck agents.
# Only track if this is the main agent and it's already trying to stop (stop_hook_active=true).
# Subagents (launched by claude itself) also trigger the stop hook, and we must not
# append to reviewed_commits for those, otherwise it looks like the main agent stopped
# again and can falsely trigger the "stuck agent" detection.
STOP_HOOK_ACTIVE=$(echo "$HOOK_INPUT" | jq -r '.stop_hook_active // false')
if [[ "$STOP_HOOK_ACTIVE" == "true" ]]; then
    ( git rev-parse HEAD || echo "conflict" ) >> .claude/reviewed_commits
fi

# Check if we've reviewed the same commit 3 times in a row (agent is stuck)
if [[ -f .claude/reviewed_commits ]]; then
    LAST_THREE=$(tail -n 3 .claude/reviewed_commits)
    ENTRY_COUNT=$(echo "$LAST_THREE" | wc -l)

    if [[ $ENTRY_COUNT -ge 3 ]]; then
        UNIQUE_COUNT=$(echo "$LAST_THREE" | sort -u | wc -l)
        if [[ $UNIQUE_COUNT -eq 1 ]]; then
            echo "ERROR: This hook has been run 3 times at the same commit." >&2
            echo "ERROR: The agent appears to be stuck and unable to make progress." >&2
            echo "ERROR: Please investigate and resolve the issue manually." >&2
            notify_user || echo "No notify_user function defined, skipping."
            exit 1
        fi
    fi
fi

# Export variables needed by child scripts
export TMUX_SESSION SCRIPT_DIR CURRENT_BRANCH BASE_BRANCH
export RED GREEN YELLOW NC

_log_to_file "INFO" "Launching child scripts in parallel..."

# Launch both scripts in parallel
"$SCRIPT_DIR/stop_hook_pr_and_ci.sh" &
PR_CI_PID=$!
_log_to_file "INFO" "Launched stop_hook_pr_and_ci.sh (pid=$PR_CI_PID)"

"$SCRIPT_DIR/stop_hook_reviewer.sh" &
REVIEWER_PID=$!
_log_to_file "INFO" "Launched stop_hook_reviewer.sh (pid=$REVIEWER_PID)"

# Kill a process and all its descendants (depth-first).
_kill_tree() {
    local pid="$1"
    local child_pids
    child_pids=$(pgrep -P "$pid" 2>/dev/null || true)
    for cpid in $child_pids; do
        _kill_tree "$cpid"
    done
    kill -9 "$pid" 2>/dev/null || true
}

# Poll until either process exits with code 2 (actionable failure) or both finish.
# Exit code 2 means the agent needs to fix something, so we return immediately
# to let it start working rather than waiting for the other hook.
PR_CI_EXIT=""
REVIEWER_EXIT=""

_log_to_file "INFO" "Entering poll loop (waiting for children to finish)..."

while true; do
    # Check PR/CI process
    if [[ -z "$PR_CI_EXIT" ]] && ! kill -0 "$PR_CI_PID" 2>/dev/null; then
        wait "$PR_CI_PID" && PR_CI_EXIT=0 || PR_CI_EXIT=$?
        _log_to_file "INFO" "PR/CI process (pid=$PR_CI_PID) exited with code $PR_CI_EXIT"
        if [[ $PR_CI_EXIT -eq 2 ]]; then
            log_error "PR/CI hook failed (exit code 2)"
            log_error "Reviewer hook is still running in the background -- go fix the tests first, then check the outputs from the reviewers."
            log_error "Run 'cat .reviews/final_issue_json/*.json' to see those issues when you're ready (after fixing CI failures)."
            log_error "And remember that you MUST fix any CRITICAL or MAJOR issues (with confidence >= 0.7) before trying again."
            _log_to_file "INFO" "Killing reviewer tree (pid=$REVIEWER_PID) before exiting"
            disown "$REVIEWER_PID" 2>/dev/null || true
            _kill_tree "$REVIEWER_PID"
            _log_to_file "INFO" "main_stop_hook exiting with code 2 (PR/CI failure)"
            exit 2
        elif [[ $PR_CI_EXIT -ne 0 ]]; then
            log_error "PR/CI hook failed (exit code $PR_CI_EXIT)"
        fi
    fi

    # Check reviewer process
    if [[ -z "$REVIEWER_EXIT" ]] && ! kill -0 "$REVIEWER_PID" 2>/dev/null; then
        wait "$REVIEWER_PID" && REVIEWER_EXIT=0 || REVIEWER_EXIT=$?
        _log_to_file "INFO" "Reviewer process (pid=$REVIEWER_PID) exited with code $REVIEWER_EXIT"
        if [[ $REVIEWER_EXIT -eq 2 ]]; then
            log_error "Reviewer hook failed (exit code 2)"
            log_error "PR/CI hook is still running in the background -- go fix the issues flagged by the reviewer first, then check back in to see if the tests passed in CI."
            log_error "When checking CI, use the gh tool to inspect the remote test results for this branch and see what failed."
            log_error "If any tests failed, remember that you MUST identify the issue and fix it locally before trying again!"
            log_error "NEVER just re-trigger the pipeline!"
            log_error "NEVER fix timeouts by increasing them! Instead, make things faster or increase parallelism."
            log_error "If it is impossible to fix the test, tell the user and say that you failed."
            log_error "Otherwise, once you have understood and fixed any issues, you can simply commit to try again."
            _log_to_file "INFO" "Killing PR/CI tree (pid=$PR_CI_PID) before exiting"
            disown "$PR_CI_PID" 2>/dev/null || true
            _kill_tree "$PR_CI_PID"
            _log_to_file "INFO" "main_stop_hook exiting with code 2 (reviewer failure)"
            exit 2
        elif [[ $REVIEWER_EXIT -ne 0 ]]; then
            log_error "Reviewer hook failed (exit code $REVIEWER_EXIT)"
        fi
    fi

    # Both finished
    if [[ -n "$PR_CI_EXIT" && -n "$REVIEWER_EXIT" ]]; then
        _log_to_file "INFO" "Both children finished: PR_CI_EXIT=$PR_CI_EXIT, REVIEWER_EXIT=$REVIEWER_EXIT"
        break
    fi

    sleep 1
done

# If either had a non-2 failure, propagate it
if [[ $PR_CI_EXIT -ne 0 ]]; then
    _log_to_file "ERROR" "main_stop_hook exiting with PR/CI exit code $PR_CI_EXIT"
    notify_user || echo "No notify_user function defined, skipping."
    exit $PR_CI_EXIT
fi
if [[ $REVIEWER_EXIT -ne 0 ]]; then
    _log_to_file "ERROR" "main_stop_hook exiting with reviewer exit code $REVIEWER_EXIT"
    notify_user || echo "No notify_user function defined, skipping."
    exit $REVIEWER_EXIT
fi

# Call local notification script if it exists
_log_to_file "INFO" "main_stop_hook completed successfully (exit 0)"
rm -f "$MNG_AGENT_STATE_DIR/active"
notify_user || echo "No notify_user function defined, skipping."

exit 0
