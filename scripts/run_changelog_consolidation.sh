#!/usr/bin/env bash
set -u
set -o pipefail

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

echo "=== consolidation start $(date -u +%FT%TZ) ==="
echo "pwd: $(pwd)"
echo "state_dir: ${MNGR_AGENT_STATE_DIR:-unset}"
which python3 && python3 --version
which claude || echo "claude NOT FOUND"
which git && git --version

write_status() {
    local status="$1"
    local pr_url_expr="$2"
    local notes="$3"
    python3 -c "
import json
json.dump({
    'status': '$status',
    'pr_url': ${pr_url_expr:-None},
    'notes': '''$notes''',
}, open('$STATUS_FILE', 'w'))
print('wrote status:', '$status')
"
}

# Step 1: deterministic consolidation
echo "=== step 1: consolidate_changelog.py ==="
CONSOLIDATE_OUTPUT=$(python3 scripts/consolidate_changelog.py 2>&1) || {
    EXIT=$?
    echo "consolidate failed (exit $EXIT): $CONSOLIDATE_OUTPUT"
    write_status "failed" "" "consolidate_changelog.py failed"
    exit 1
}
echo "$CONSOLIDATE_OUTPUT"

if echo "$CONSOLIDATE_OUTPUT" | grep -q "No changelog entries"; then
    write_status "skipped-no-entries" "" "No changelog entries to consolidate"
    exit 0
fi

# Step 2: extract the new section and ask claude for a summary
echo "=== step 2: extract new section + claude summary ==="
NEW_SECTION=$(python3 -c "
import re
content = open('UNABRIDGED_CHANGELOG.md').read()
match = re.search(r'(## \d{4}-\d{2}-\d{2}\n.*?)(?=\n## |\Z)', content, re.DOTALL)
print(match.group(1) if match else '')
")

if [ -z "$NEW_SECTION" ]; then
    echo "no new section found"
    write_status "failed" "" "Could not find newly-added section"
    exit 1
fi

echo "new section head:"
echo "$NEW_SECTION" | head -5

echo "invoking claude..."
SUMMARY=$(claude --print --dangerously-skip-permissions -p "Produce a concise, human-friendly summary of these changelog entries. Group related changes, use natural language, and keep it to a few bullet points. Output ONLY the markdown bullets, no preamble:

$NEW_SECTION" 2>&1) || {
    echo "claude failed: $SUMMARY"
    write_status "failed" "" "claude invocation failed"
    exit 1
}

if [ -z "$SUMMARY" ]; then
    echo "claude returned empty"
    write_status "failed" "" "claude returned empty summary"
    exit 1
fi
echo "summary head:"
echo "$SUMMARY" | head -5

# Step 3: update CHANGELOG.md
echo "=== step 3: update CHANGELOG.md ==="
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
print('updated CHANGELOG.md')
PY

# Step 4: commit, push, open PR
echo "=== step 4: commit + push + PR ==="
DATE_STR=$(echo "$DATE_HEADING" | sed 's/## //')

# Configure git identity + gh auth for push (no ssh in container)
git config user.email "changelog-bot@imbue.com" || true
git config user.name "Changelog Bot" || true
gh auth setup-git || echo "gh auth setup-git failed (continuing)"

git add -A
git commit -m "Consolidate changelog entries for $DATE_STR"

# Use a fresh branch name so push doesn't require force / conflict with existing
BRANCH="mngr/changelog-consolidation-$(date -u +%Y-%m-%d-%H-%M-%S)"
git checkout -b "$BRANCH"
git push --set-upstream origin "$BRANCH"

PR_OUT=$(gh pr create --base main --title "Changelog consolidation $DATE_STR" --body "Automated nightly consolidation of changelog entries." 2>&1)
echo "$PR_OUT"
PR_URL=$(echo "$PR_OUT" | grep -oE 'https://github.com/[^ ]+')

write_status "done" "'$PR_URL'" "Opened PR for $DATE_STR"
echo "=== done: $PR_URL ==="
