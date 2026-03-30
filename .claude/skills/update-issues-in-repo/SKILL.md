---
name: update-issues-in-repo
description: Convert a file containing identified issues into a tracked file in current_tasks/. Use after running identify-* commands to create a local record of current issues.
---

# Updating Issues in Repository

This skill provides guidelines for converting issue files (created by the identify-* commands) into tracked issue files stored directly in the repository.

## Overview

Issue files are markdown files located in `_tasks/<category>/` directories within a library (these are gitignored). This command converts them into tracked files at `current_tasks/<category>.md` within the same library, which are committed to the repository.

## Process

Follow these steps to convert an issue file into a tracked repository file:

### 1. Find the Newly Created Issue File

Look for recently created markdown files in the library's `_tasks/` directory. The file will be in a subdirectory that indicates the issue category:

- `_tasks/inconsistencies/` - Code inconsistencies
- `_tasks/docs/` - Documentation and code disagreements
- `_tasks/style/` - Style guide violations
- `_tasks/docstrings/` - Outdated docstrings

### 2. Determine the Category and Output Path

The category is determined by the folder name under `_tasks/` where the issue file was created. The output file should be:

- Input: `libs/<library>/_tasks/inconsistencies/<date>.md`
- Output: `libs/<library>/current_tasks/inconsistencies.md`

Create the `current_tasks/` directory in the library if it doesn't exist.

### 3. Load the Existing Issues File (if any)

If `current_tasks/<category>.md` already exists, read its contents to identify existing issues that may need updating or merging.

### 4. Parse Issues from Both Files

Issues in the markdown files follow this format:

```markdown
## <number>. <Short description>

Description: <detailed description>

Recommendation: <recommendation>

Decision: <Accept|Reject|Pending>
```

Parse all issues from:
1. The new issue file (from `_tasks/<category>/`)
2. The existing `current_tasks/<category>.md` file (if it exists)

### 5. Merge Issues

For each issue in the new file:
1. Check if a similar issue exists in the current file by comparing titles and descriptions
2. If a matching issue exists, update it with any new information from the new file
3. If no matching issue exists, add it as a new issue

For issues that exist in the current file but not in the new file:
- Keep them (they may still be valid, just not re-identified this time)

### 6. Sort and Number Issues

Sort all issues by importance (same criteria as the identify-* commands use):
- Impact (higher = more important)
- Certainty (higher = more important)
- Effort to fix (lower = more important)

Renumber all issues starting from 1.

### 7. Write the Output File

Write the merged, sorted issues to `current_tasks/<category>.md` with the format:

```markdown
# Current <Category> Issues

Last updated: <current date/time>

## 1. <Short description>

Description: <detailed description>

Recommendation: <recommendation>

Decision: Accept

## 2. <Short description>

...
```

### 8. Commit the Changes

Commit the updated file with a descriptive message:

```bash
git add libs/<library>/current_tasks/<category>.md
git commit -m "Update current <category> issues for <library>"
```

### 9. Fetch and Merge Main

Before pushing, ensure you have the latest changes from main:

```bash
git fetch origin main
git merge origin/main --no-edit
```

If there are merge conflicts in the `current_tasks/` file, resolve them by keeping the most comprehensive version of each issue.

### 10. Push Directly to Main

Since this only contains documentation updates (the current issues list), push directly to main:

```bash
git push origin HEAD:main
```

## Important Notes

- The `current_tasks/` directory should NOT be gitignored (unlike `_tasks/`)
- This provides a quick reference of current known issues without needing GitHub
- The file is meant to be a living document that gets updated as issues are identified and resolved
- When issues are fixed, they can be removed from the file manually or by running this command again with an updated source file
- The "Decision" field can be set to "Accept" (will be fixed), "Reject" (not a real issue), or "Pending" (needs discussion)
