#!/usr/bin/env bash
set -euo pipefail

# Idempotent setup of the nightly changelog consolidation agent.
#
# This script ensures exactly one "changelog-consolidation" schedule exists.
# Safe to run multiple times: skips creation if the schedule already exists,
# so there is never a risk of duplicate agents.
#
# The scheduled agent runs at midnight PST, consolidates per-PR changelog
# entries into UNABRIDGED_CHANGELOG.md via the deterministic script
# (scripts/consolidate_changelog.py), then writes a concise AI-generated
# summary to CHANGELOG.md, commits, and opens a PR.
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
# script) to consolidate entries into UNABRIDGED_CHANGELOG.md, then updates
# CHANGELOG.md with a concise AI-generated summary, commits, and opens a PR.
uv run mngr schedule add "$TRIGGER_NAME" \
    --command create \
    --schedule "$SCHEDULE" \
    --provider "$PROVIDER" \
    --no-ensure-safe-commands \
    --args '--type claude --branch :mngr/changelog-consolidation-{DATE} --message "Step 1: Run uv run python scripts/consolidate_changelog.py to consolidate changelog entries into UNABRIDGED_CHANGELOG.md. If it reports no entries, exit without changes. Step 2: Read the new section that was just added to UNABRIDGED_CHANGELOG.md (the topmost ## section). Then update CHANGELOG.md by adding a concise, human-friendly summary of these changes under the same date heading, inserted after the header text. Group related changes, use natural language, and keep it brief. Step 3: Commit all changed files. Step 4: Open a PR targeting main with a title like Changelog consolidation YYYY-MM-DD."'

echo "Schedule '${TRIGGER_NAME}' created successfully."
