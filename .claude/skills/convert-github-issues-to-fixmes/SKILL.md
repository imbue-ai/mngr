---
name: convert-github-issues-to-fixmes
description: Convert triaged autoclaude GitHub issues into FIXMEs in the codebase. Use when you want to process issues that have been triaged by authorized users.
---

# Converting GitHub Issues to FIXMEs

This skill provides guidelines for converting triaged "autoclaude" GitHub issues into FIXME comments in the codebase.

## Overview

Issues created by the `create-github-issues` skill are labeled with "autoclaude". After human triage (via comments from authorized users), these issues should be converted into FIXMEs in the code, or marked as non-issues.

## Prerequisites

The authorized users list is maintained in `scripts/authorized_github_users.toml`. This file contains an array of GitHub usernames whose comments are considered authoritative for triage decisions.

## Process

### 1. Load Issues and Filter by Authorized Users

Run the helper script to fetch all open "autoclaude" issues and their comments, filtering to only include comments from authorized users:

```bash
./scripts/load_triaged_issues.sh > triaged_issues.json
```

This script (requires `gh` and `jq`):
1. Loads the list of authorized users from `scripts/authorized_github_users.toml`
2. Fetches all open issues with the "autoclaude" label
3. For each issue, fetches comments and filters to only those from authorized users
4. Outputs only issues that have at least one comment from an authorized user
5. Writes the filtered data as JSON to stdout

### 2. Check If There Are Any Issues to Process

Read the `triaged_issues.json` file. If the `issues` array is empty (i.e., `{"issues": []}`), there are no triaged issues to process. Stop here.

### 3. Create a Working Branch

Create a branch off of main with the naming convention:

```bash
git checkout main
git pull origin main
git checkout -b "mngr/add-fixmes-$(date +%Y%m%d%H%M%S)"
```

### 4. Process Each Issue

For each issue in the triaged issues list:

#### 4a. Determine the Action

Look at the **last** comment from any authorized user:

- If the last comment text (trimmed and lowercased) is exactly "ignore": Add to `non_issues.md`
- Otherwise: Create a FIXME in the code

#### 4b. For "ignore" Comments - Add to non_issues.md

Open the `non_issues.md` file in the relevant sub-project (e.g., `libs/mngr/non_issues.md`) and add a single line describing why this is not an issue. The line should:

- Be concise (one sentence)
- Reference the original issue content to prevent it from being flagged again
- Follow the existing format in the file

Example:
```markdown
- using default arguments in CLI option parsing is intentional for usability (issue #123)
```

#### 4c. For Other Comments - Create a FIXME

Transfer the issue data into a `# FIXME` comment in the correct location in the codebase:

1. Identify the file and line number from the issue body
2. Navigate to that location
3. Add a FIXME comment that includes:
   - The issue title as a summary
   - Key details from the issue description
   - Any relevant guidance from the authorized user's comment(s)
   - A reference to the original issue number

Example FIXME format:
```python
# FIXME(#123): Short description from issue title
# Details: Key information from issue body
# Guidance: Any relevant notes from triage comments
```

Make sure to:
- Place the FIXME at the correct file and line
- Include all relevant information
- Keep the comment concise but complete

### 5. Commit Changes

After processing all issues, commit the changes:

```bash
git add -A
git commit -m "Add FIXMEs and update non_issues.md from triaged GitHub issues"
```

### 6. Create a Pull Request

Push the branch and create a PR:

```bash
git push -u origin HEAD
gh pr create --title "Add FIXMEs from triaged GitHub issues" --body "$(cat <<'EOF'
## Summary
- Converted triaged autoclaude issues into FIXMEs
- Updated non_issues.md for issues marked as 'ignore'

## Issues Processed
See individual commits for details on each issue processed.

Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

### 7. Close the Processed Issues

For each issue that was processed, close it with a comment linking to the PR:

```bash
gh issue close <issue_number> --comment "Processed in PR #<pr_number>"
```

## Notes

- Only issues with comments from authorized users are considered "triaged"
- The authorized users list should be maintained by project administrators
- Always create a new branch - never commit directly to main
- Each issue should be fully processed (either FIXME created or added to non_issues.md) before moving to the next
