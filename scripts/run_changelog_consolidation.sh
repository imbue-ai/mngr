#!/usr/bin/env bash
set -euo pipefail

# Nightly changelog consolidation script.
#
# Runs deterministic consolidation, uses claude for an AI-generated summary,
# commits, pushes, and opens a PR. Writes a machine-readable status.json to
# $MNGR_AGENT_STATE_DIR so callers can check the result via `mngr file get`
# even after the ephemeral sandbox exits.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

STATUS_FILE="${MNGR_AGENT_STATE_DIR:-/tmp}/status.json"

write_status() {
    local status="$1"
    local pr_url="$2"
    local notes="$3"
    python3 -c "
import json, sys
json.dump({
    'status': '$status',
    'pr_url': ${pr_url:-None},
    'notes': '''$notes''',
}, open('$STATUS_FILE', 'w'))
"
    echo "Wrote status to $STATUS_FILE"
}

# Step 1: deterministic consolidation
OUTPUT=$(uv run python scripts/consolidate_changelog.py 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -q "No changelog entries"; then
    write_status "skipped-no-entries" "" "No changelog entries to consolidate"
    exit 0
fi

# Step 2: extract the new section that was just added and ask claude for summary
NEW_SECTION=$(python3 -c "
import re
content = open('UNABRIDGED_CHANGELOG.md').read()
match = re.search(r'(## \d{4}-\d{2}-\d{2}\n.*?)(?=\n## |\Z)', content, re.DOTALL)
print(match.group(1) if match else '')
")

if [ -z "$NEW_SECTION" ]; then
    write_status "failed" "" "Could not find newly-added section in UNABRIDGED_CHANGELOG.md"
    exit 1
fi

SUMMARY=$(claude --print --dangerously-skip-permissions -p "Produce a concise, human-friendly summary of these changelog entries. Group related changes, use natural language, and keep it to a few bullet points. Output ONLY the markdown bullets, no preamble:

$NEW_SECTION")

if [ -z "$SUMMARY" ]; then
    write_status "failed" "" "claude returned empty summary"
    exit 1
fi

# Step 3: prepend summary to CHANGELOG.md under same date heading
DATE_HEADING=$(echo "$NEW_SECTION" | head -1)
python3 <<PY
from pathlib import Path
p = Path('CHANGELOG.md')
existing = p.read_text() if p.exists() else '# Changelog\n\n'
lines = existing.split('\n')
insert = len(lines)
for i, line in enumerate(lines):
    if line.startswith('## '):
        insert = i
        break
new_section = """$DATE_HEADING

$SUMMARY
"""
before = '\n'.join(lines[:insert]).rstrip() + '\n\n'
after = '\n'.join(lines[insert:])
p.write_text(before + new_section + '\n' + after)
PY

# Step 4: commit, push, open PR
DATE_STR=$(echo "$DATE_HEADING" | sed 's/## //')
git add -A
git commit -m "Consolidate changelog entries for $DATE_STR"
git push origin HEAD

PR_URL=$(gh pr create --base main --title "Changelog consolidation $DATE_STR" --body "Automated nightly consolidation of changelog entries." 2>&1 | grep -oE 'https://github.com/[^ ]+')

write_status "done" "'$PR_URL'" "Opened PR for $DATE_STR"
echo "Done. PR: $PR_URL"
