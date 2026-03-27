#!/usr/bin/env bash
#
# main_claude_stop_hook.sh
#
# Orchestrator for stop hook scripts. Performs shared setup (precondition
# checks, fetch/merge/push, informational detection, stuck-agent tracking),
# then gates on autofix verification and launches stop_hook_pr_and_ci.sh
# to handle PR creation and CI checks.

set -euo pipefail

# Drain stdin so downstream commands don't accidentally consume the hook JSON
cat > /dev/null 2>&1 || true

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
# ID to $MNGR_AGENT_STATE_DIR/claude_session_id so we can track it here.
if [ -n "${MNGR_AGENT_STATE_DIR:-}" ] && [ -f "$MNGR_AGENT_STATE_DIR/claude_session_id" ]; then
    _MNGR_READ_SID=$(cat "$MNGR_AGENT_STATE_DIR/claude_session_id")
    if [ -n "$_MNGR_READ_SID" ]; then
        MAIN_CLAUDE_SESSION_ID="$_MNGR_READ_SID"
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
if [[ -n "${MNGR_AGENT_STATE_DIR:-}" ]]; then
    mkdir -p "$MNGR_AGENT_STATE_DIR/events/logs/stop_hook" 2>/dev/null || true
    export STOP_HOOK_LOG="$MNGR_AGENT_STATE_DIR/events/logs/stop_hook/events.jsonl"
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
# Set to true when the safety hatch fires so the EXIT trap does not
# immediately re-create the stuck file (which would undo the rm -f).
_STUCK_HATCH_FIRED=false

trap '
    _exit_code=$?
    _log_to_file "INFO" "main_stop_hook EXIT trap fired (pid=$$, exit_code=$_exit_code)"
    # Track blocked attempts for stuck agent detection (skip if the
    # safety hatch already fired -- otherwise rm + exit + trap creates
    # an infinite 3-block cycle)
    if [[ $_exit_code -ne 0 ]] && [[ "$_STUCK_HATCH_FIRED" != "true" ]]; then
        mkdir -p "$(dirname "$STUCK_FILE")" 2>/dev/null || true
        echo "$HASH" >> "$STUCK_FILE" 2>/dev/null || true
    fi
' EXIT

# ---------------------------------------------------------------------------
# Stuck agent detection for the FULL stop hook (gates + CI + PR).
#
# stop_hook_gates.sh has its own safety hatch for gate-only loops (used by
# both this script and the plugin's standalone Stop hook). This separate
# tracker covers failures in the CI/PR stages that happen AFTER gates pass.
# ---------------------------------------------------------------------------
STUCK_FILE=".claude/blocked_stop_commits"
HASH=$(git rev-parse HEAD 2>/dev/null || echo "unknown")

_check_stuck() {
    if [[ ! -f "$STUCK_FILE" ]]; then
        return 1
    fi
    local last_three entry_count unique_count
    last_three=$(tail -n 3 "$STUCK_FILE")
    entry_count=$(echo "$last_three" | wc -l | tr -d ' ')
    if [[ $entry_count -ge 3 ]]; then
        unique_count=$(echo "$last_three" | sort -u | wc -l | tr -d ' ')
        if [[ $unique_count -eq 1 ]]; then
            return 0
        fi
    fi
    return 1
}

if _check_stuck; then
    log_error "Stop hook has blocked 3 times at the same commit ($HASH)."
    log_error "The agent appears stuck. Please investigate manually."
    _log_to_file "ERROR" "Stuck agent detected at $HASH, exiting with error"
    _STUCK_HATCH_FIRED=true
    rm -f "$STUCK_FILE"
    notify_user || echo "No notify_user function defined, skipping."
    exit 1
fi

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
    rm -f "$MNGR_AGENT_STATE_DIR/active"
    exit 0
fi

# Export variables needed by child scripts
export TMUX_SESSION SCRIPT_DIR CURRENT_BRANCH BASE_BRANCH
export RED GREEN YELLOW NC

# ---------------------------------------------------------------------------
# Gate checks: autofix and conversation review must both pass before we
# bother launching the (slow) CI check.
# ---------------------------------------------------------------------------
if GATE_OUTPUT=$("$SCRIPT_DIR/stop_hook_gates.sh" "$HASH" 2>&1); then
    _log_to_file "INFO" "Autofix and conversation review gates passed"
else
    while IFS= read -r line; do
        log_error "$line"
    done <<< "$GATE_OUTPUT"
    _log_to_file "INFO" "main_stop_hook exiting with code 2 (gate check failed)"
    exit 2
fi

# PR/CI gate (can be disabled via .reviewer/settings.json)
source "$SCRIPT_DIR/config_utils.sh"
REVIEWER_SETTINGS=".reviewer/settings.json"
CI_ENABLED=$(read_json_config "$REVIEWER_SETTINGS" "ci.is_enabled" "true")

if [[ "$CI_ENABLED" != "true" ]]; then
    _log_to_file "INFO" "PR/CI check disabled via .reviewer/settings.json, skipping"
    log_info "PR/CI check disabled (ci.is_enabled=false in .reviewer/settings.json), skipping."
else
    _log_to_file "INFO" "Launching CI check..."

    # Launch the PR/CI check (the only remaining async child)
    "$SCRIPT_DIR/stop_hook_pr_and_ci.sh" &
    PR_CI_PID=$!
    _log_to_file "INFO" "Launched stop_hook_pr_and_ci.sh (pid=$PR_CI_PID)"

    wait "$PR_CI_PID" && PR_CI_EXIT=0 || PR_CI_EXIT=$?
    _log_to_file "INFO" "PR/CI process (pid=$PR_CI_PID) exited with code $PR_CI_EXIT"

    if [[ $PR_CI_EXIT -eq 2 ]]; then
        log_error "PR/CI hook failed -- go fix the CI failures first."
        log_error "If autofix has run, check .reviewer/outputs/autofix/issues/*.jsonl for identified issues."
        _log_to_file "INFO" "main_stop_hook exiting with code 2 (PR/CI failure)"
        exit 2
    elif [[ $PR_CI_EXIT -ne 0 ]]; then
        log_error "PR/CI hook failed (exit code $PR_CI_EXIT)"
        _log_to_file "ERROR" "main_stop_hook exiting with PR/CI exit code $PR_CI_EXIT"
        notify_user || echo "No notify_user function defined, skipping."
        exit "$PR_CI_EXIT"
    fi
fi

# Success -- clear stuck tracking and upload issue data
rm -f "$STUCK_FILE"

# Upload autofix issue data to Modal volume for data collection (best-effort).
_upload_autofix_issues() {
    local issues_dir=".reviewer/outputs/autofix/issues"
    if [[ ! -d "$issues_dir" ]] || ! ls "$issues_dir"/*.jsonl >/dev/null 2>&1; then
        _log_to_file "INFO" "No autofix issue files to upload"
        return
    fi

    local commit
    commit=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
    # Nested directory path from commit hash (same structure as old reviewer)
    local nested_path="${commit:0:4}/${commit:4:4}/${commit:8:4}/${commit:12:4}/${commit:16}"

    local volume_name="code-review-json"
    local volume_mount="/code_reviews"

    # Concatenate all issue files for upload
    local combined
    combined=$(mktemp)
    cat "$issues_dir"/*.jsonl > "$combined"

    # Method 1: Copy to mounted volume + sync (Modal sandbox)
    local mount_dir="${volume_mount}/${nested_path}"
    if mkdir -p "${mount_dir}" 2>/dev/null && cp "$combined" "${mount_dir}/autofix.json" 2>/dev/null; then
        if sync "${volume_mount}" 2>/dev/null; then
            log_info "Uploaded autofix issues to mounted volume at ${mount_dir}/autofix.json"
        else
            log_warn "Copied to mounted volume but sync failed"
        fi
    else
        _log_to_file "INFO" "Direct volume copy failed (expected if not running in Modal)"
    fi

    # Method 2: Upload via modal CLI (local machine with Modal credentials)
    if uv run modal volume put "${volume_name}" "$combined" "/${nested_path}/autofix.json" --force 2>/dev/null; then
        log_info "Uploaded autofix issues via modal volume put"
    else
        _log_to_file "INFO" "modal volume put failed (expected if not running locally with Modal credentials)"
    fi

    rm -f "$combined"
}
_upload_autofix_issues

_log_to_file "INFO" "main_stop_hook completed successfully (exit 0)"
rm -f "$MNGR_AGENT_STATE_DIR/active"
notify_user || echo "No notify_user function defined, skipping."

exit 0
