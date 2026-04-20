#!/usr/bin/env bash
set -euo pipefail

# Idempotent setup of the nightly changelog consolidation agent.
#
# This script ensures exactly one "changelog-consolidation" schedule exists.
# Safe to run multiple times: skips creation if the schedule already exists.
#
# The scheduled agent runs at midnight PST as a headless_claude agent that:
#   1. Runs scripts/consolidate_changelog.py (deterministic consolidation)
#   2. Summarizes the new section into CHANGELOG.md
#   3. Commits, pushes a fresh branch, and opens a PR
#   4. Writes status.json to $MNGR_AGENT_STATE_DIR for post-hoc inspection
#
# Usage:
#   ./scripts/setup_changelog_agent.sh
#
# Environment:
#   CHANGELOG_PROVIDER  - Provider to use (default: "modal").
#   CHANGELOG_VERIFY    - Verification mode (default: "none"). Set to "quick"
#                         or "full" to run the agent once during deploy.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

TRIGGER_NAME="changelog-consolidation"
# Midnight PST (UTC-8) = 08:00 UTC.
SCHEDULE="0 8 * * *"
PROVIDER="${CHANGELOG_PROVIDER:-modal}"
VERIFY="${CHANGELOG_VERIFY:-none}"

# Use an isolated mngr config namespace so we don't load the repo's
# .mngr/settings.toml (which references plugins that won't exist in the
# container). Mirrors test_schedule_run.py's build_subprocess_env pattern.
export MNGR_ROOT_NAME="mngr-changelog-schedule"
unset MNGR_HOST_DIR
unset MNGR_PREFIX

# Validate that required env vars are set so the agent can function.
for var in GH_TOKEN ANTHROPIC_API_KEY; do
    if [ -z "${!var:-}" ]; then
        echo "Error: $var is not set. The scheduled agent needs this to operate." >&2
        exit 1
    fi
done

# IS_SANDBOX=1 lets claude accept --dangerously-skip-permissions as root
# inside the Modal container.
export IS_SANDBOX=1

# Compute --disable-plugin args for every installed plugin EXCEPT the
# minimum set the scheduled run needs.
DISABLE_PLUGIN_ARGS=$(uv run python -c "
import importlib.metadata
enabled = {'schedule', 'modal', 'claude', 'headless_claude', 'file'}
names = sorted({ep.name for ep in importlib.metadata.entry_points(group='mngr')} - enabled)
print(' '.join(f'--disable-plugin {n}' for n in names))
")

# Check if the trigger already exists. Error unless CHANGELOG_REPLACE=1 was
# set, since the user probably wants to know they're about to clobber a live
# schedule.
EXISTING=$(uv run mngr schedule list --provider "$PROVIDER" --all --format json $DISABLE_PLUGIN_ARGS 2>/dev/null || echo '{"schedules":[]}')
if echo "$EXISTING" | python3 -c "
import json, sys
data = json.load(sys.stdin)
names = [s['trigger']['name'] for s in data.get('schedules', [])]
sys.exit(0 if '${TRIGGER_NAME}' in names else 1)
" 2>/dev/null; then
    if [ "${CHANGELOG_REPLACE:-}" != "1" ]; then
        echo "Error: Schedule '${TRIGGER_NAME}' already exists on provider '$PROVIDER'." >&2
        echo "       Set CHANGELOG_REPLACE=1 to remove the existing schedule and redeploy." >&2
        exit 1
    fi
    echo "CHANGELOG_REPLACE=1 set. Removing existing schedule before redeploy..."
    uv run mngr schedule remove "$TRIGGER_NAME" --provider "$PROVIDER" --force $DISABLE_PLUGIN_ARGS
fi

echo "Creating schedule '${TRIGGER_NAME}' (provider=$PROVIDER, verify=$VERIFY)..."

# The agent's prompt drives the full workflow. It invokes the deterministic
# consolidation script, writes the AI summary, commits, pushes a fresh branch,
# opens a PR, and writes a machine-readable status.json to its state dir.
PROMPT=$(cat <<'EOF'
You are the nightly changelog consolidation agent. You are already on a fresh
branch checked out from main -- just commit onto it and push. Steps:

1. Run: uv run python scripts/consolidate_changelog.py
   - If the output contains "No changelog entries", skip to step 6 and write status="skipped-no-entries" with pr_url=null.
   - If it fails, skip to step 6 and write status="failed" with pr_url=null and notes describing the error.

2. Read UNABRIDGED_CHANGELOG.md and extract the topmost ## section (the one the script just added).

3. Write a concise, human-friendly summary of that section into CHANGELOG.md: prepend a new section under the same date heading, after the existing header text, before any older ## sections. Group related changes, use natural language, keep it to a few bullets.

4. Configure git and commit:
   - git config user.email "changelog-bot@imbue.com"
   - git config user.name "Changelog Bot"
   - gh auth setup-git
   - git add -A
   - git commit -m "Consolidate changelog entries for <today's date>"
   - git push --set-upstream origin HEAD

5. Open a PR with: gh pr create --base main --title "Changelog consolidation <today's date>" --body "<body>"
   - The body should start with "Automated nightly consolidation of changelog entries."
   - If anything looked weird or wrong during the run (malformed entries, conflicts, unexpected consolidation output, errors you worked around, etc.), append a second paragraph with those notes so a human can review. If everything was clean, one sentence is fine.
   - Capture the PR URL from the output.

6. Write status.json to the agent state directory ($MNGR_AGENT_STATE_DIR/status.json). The file must be valid JSON with keys:
   - status: one of "done", "skipped-no-entries", "failed"
   - pr_url: the PR URL string if a PR was opened, else null
   - notes: a short sentence about what happened
EOF
)

uv run mngr schedule add "$TRIGGER_NAME" \
    --command create \
    --schedule "$SCHEDULE" \
    --provider "$PROVIDER" \
    --verify "$VERIFY" \
    --full-copy \
    --no-auto-merge \
    --exclude-user-settings \
    --exclude-project-settings \
    --pass-env GH_TOKEN \
    --pass-env ANTHROPIC_API_KEY \
    --pass-env MNGR_ROOT_NAME \
    --pass-env IS_SANDBOX \
    --no-auto-fix-args \
    $DISABLE_PLUGIN_ARGS \
    --args "--type headless_claude --foreground --no-ensure-clean --branch :mngr/changelog-consolidation-{DATE} --host-label SCHEDULE=$TRIGGER_NAME --message $(printf %s "$PROMPT" | uv run python -c 'import shlex, sys; print(shlex.quote(sys.stdin.read()), end="")')"

echo "Schedule '${TRIGGER_NAME}' created successfully."
echo ""
echo "To check the result of a run (works even if sandbox has exited):"
echo "  uv run mngr list --format json $DISABLE_PLUGIN_ARGS"
echo "  uv run mngr file get <agent-id> status.json --relative-to state $DISABLE_PLUGIN_ARGS"
