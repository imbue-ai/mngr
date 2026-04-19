#!/usr/bin/env bash
set -euo pipefail

# Idempotent setup of the nightly changelog consolidation agent.
#
# This script ensures exactly one "changelog-consolidation" schedule exists.
# Safe to run multiple times: skips creation if the schedule already exists.
#
# The scheduled agent runs at midnight PST, executes
# scripts/run_changelog_consolidation.sh (which consolidates entries, uses
# claude for an AI-generated summary, commits, pushes, and opens a PR), and
# writes a machine-readable status.json to the agent state dir so callers can
# check the result via `mngr file get` even after the Modal sandbox exits.
#
# Usage:
#   ./scripts/setup_changelog_agent.sh
#
# Environment:
#   CHANGELOG_PROVIDER  - Provider to use (default: "modal").
#   CHANGELOG_VERIFY    - Verification mode (default: "full"). Set to "none"
#                         or "quick" to skip/shorten post-deploy verification.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

TRIGGER_NAME="changelog-consolidation"
# Midnight PST (UTC-8) = 08:00 UTC.
# During PDT (March-November), this fires at 01:00 local time.
SCHEDULE="0 8 * * *"
PROVIDER="${CHANGELOG_PROVIDER:-modal}"
VERIFY="${CHANGELOG_VERIFY:-full}"

# Use an isolated mngr config namespace both locally and inside the container,
# so neither loads the user's personal settings or the repo's .mngr/settings.toml
# (which references providers/plugins that may not exist in the container).
# Mirrors the pattern used by test_schedule_run.py's build_subprocess_env.
export MNGR_ROOT_NAME="mngr-changelog-schedule"

# Validate that required env vars are set so the agent can function.
for var in GH_TOKEN ANTHROPIC_API_KEY; do
    if [ -z "${!var:-}" ]; then
        echo "Error: $var is not set. The scheduled agent needs this to operate." >&2
        exit 1
    fi
done

# Compute --disable-plugin args for every installed plugin EXCEPT the minimum
# set the scheduled run needs. This avoids config-parse errors for plugins that
# have fields in the repo's settings.toml that the container mngr doesn't know.
# Needed plugins: schedule (deploy mechanism), modal (runtime provider),
# headless_command (the agent type that runs our bash script).
DISABLE_PLUGIN_ARGS=$(python3 -c "
import importlib.metadata
enabled = {'schedule', 'modal', 'headless_command'}
names = sorted({ep.name for ep in importlib.metadata.entry_points(group='mngr')} - enabled)
print(' '.join(f'--disable-plugin {n}' for n in names))
")

# Check if the trigger already exists.
EXISTING=$(uv run mngr schedule list --provider "$PROVIDER" --all --format json $DISABLE_PLUGIN_ARGS 2>/dev/null || echo '{"schedules":[]}')
if echo "$EXISTING" | python3 -c "
import json, sys
data = json.load(sys.stdin)
names = [s['trigger']['name'] for s in data.get('schedules', [])]
sys.exit(0 if '${TRIGGER_NAME}' in names else 1)
" 2>/dev/null; then
    echo "Schedule '${TRIGGER_NAME}' already exists. No action needed."
    exit 0
fi

echo "Creating schedule '${TRIGGER_NAME}' (provider=$PROVIDER, verify=$VERIFY)..."

# headless_command with --foreground makes mngr create run synchronously
# inside the container and stream stdout until the command exits. This keeps
# the Modal container alive until our consolidation script finishes. The
# script writes status.json to $MNGR_AGENT_STATE_DIR which persists on the
# Modal state volume and can be read via `mngr file get` afterward.
#
# -S passes a config override so headless_command runs our bash script.
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
    --no-auto-fix-args \
    --no-ensure-safe-commands \
    $DISABLE_PLUGIN_ARGS \
    --args "--type headless_command --foreground -S agent_types.headless_command.command='bash scripts/run_changelog_consolidation.sh'"

echo "Schedule '${TRIGGER_NAME}' created successfully."
echo ""
echo "To check the result of a run (works even if sandbox has exited):"
echo "  uv run mngr list --format json $DISABLE_PLUGIN_ARGS"
echo "  uv run mngr file get <agent-id> status.json --relative-to state $DISABLE_PLUGIN_ARGS"
