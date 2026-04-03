#!/usr/bin/env bash
set -euo pipefail

# Idempotent setup of the nightly changelog consolidation agent.
#
# This script ensures exactly one "changelog-consolidation" schedule exists.
# Safe to run multiple times: skips creation if the schedule already exists,
# so there is never a risk of duplicate agents.
#
# The scheduled agent runs at midnight PST, executes the deterministic
# consolidation script (scripts/consolidate_changelog.py), commits the
# result, and opens a PR.
#
# Usage:
#   ./scripts/setup_changelog_agent.sh
#
# Environment:
#   CHANGELOG_PROVIDER  - Provider to use (default: "local"). Set to "modal"
#                         for production use (requires Modal credentials).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

TRIGGER_NAME="changelog-consolidation"
# Midnight PST (UTC-8) = 08:00 UTC.
# During PDT (March-November), this fires at 01:00 local time.
SCHEDULE="0 8 * * *"
PROVIDER="${CHANGELOG_PROVIDER:-local}"

# Check if the trigger already exists by listing schedules as JSON.
EXISTING=$(uv run mngr schedule list --provider "$PROVIDER" --all --format json 2>/dev/null || echo '{"schedules":[]}')
if echo "$EXISTING" | python3 -c "
import json, sys
data = json.load(sys.stdin)
names = [s['trigger']['name'] for s in data.get('schedules', [])]
sys.exit(0 if '${TRIGGER_NAME}' in names else 1)
" 2>/dev/null; then
    echo "Schedule '${TRIGGER_NAME}' already exists. No action needed."
    exit 0
fi

echo "Creating schedule '${TRIGGER_NAME}'..."

# The agent runs scripts/consolidate_changelog.py (a deterministic Python
# script) then commits and lets the stop hook create the PR.
uv run mngr schedule add "$TRIGGER_NAME" \
    --command create \
    --schedule "$SCHEDULE" \
    --provider "$PROVIDER" \
    --no-ensure-safe-commands \
    --args '--type claude --branch :mngr/changelog-consolidation-{DATE} --message "Run the changelog consolidation script: uv run python scripts/consolidate_changelog.py. If it reports no entries to consolidate, exit without changes. Otherwise, commit the updated CHANGELOG.md and the deleted changelog files."'

echo "Schedule '${TRIGGER_NAME}' created successfully."
