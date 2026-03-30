---
name: fix-issue
argument-hint: [issue]
description: Fix a GitHub issue given its number or URL. Replicates the bug, finds root cause, implements a fix, and opens a PR.
---

# Fix a GitHub Issue

You are given a GitHub issue to fix: `$1`

## 1. Understand the issue

Fetch the issue details:

```bash
gh issue view $1 --json number,title,body,labels,comments
```

Read it carefully. If the issue is misguided, unclear to the point of being unactionable, or describes something that is already fixed, comment on the issue explaining why (prefix your comment with `[fix-issue]`) and stop.

## 2. Reproduce and investigate (in parallel)

First, gather context for the relevant library (per the "How to get started" instructions in CLAUDE.md).

Then launch two efforts concurrently:

- **Reproduce**: Write a minimal reproduction (test, script, or manual steps) that demonstrates the bug or missing behavior. If the issue is a feature request rather than a bug, skip reproduction and instead write a failing test that captures the desired behavior.
- **Root-cause search**: Read the relevant code, trace the control flow, and identify where the fix should go.

If you cannot reproduce the bug or identify a root cause, comment on the issue explaining what you tried and what you found (prefix with `[fix-issue]`) and stop.

## 3. Decide on approach

If there is a clear, unambiguous fix, just do it.

If the fix involves a meaningful architectural choice (e.g., where to put new logic, which abstraction to use, changing public interfaces), stop and ask the user before proceeding. Briefly lay out the options and your recommendation.

## 4. Implement the fix

- Fix the root cause.
- Add or update tests to cover the fix.
- Get all tests passing (`uv run pytest` in the relevant project directory).

## 5. Commit and open a PR

Commit your changes with a message that references the issue (e.g., "Fix <description> (#<issue_number>)").

Note: CLAUDE.md says not to create PRs yourself. Ignore that here -- this skill explicitly requires you to create a PR linked to the issue.

Create a branch if you're not already on one, push it, then open a PR that closes the issue:

```bash
gh pr create --title "<concise title>" --body "$(cat <<'EOF'
Closes #<issue_number>

## Summary
<what changed and why>

## Test plan
<how the fix is verified>
EOF
)"
```

The `Closes #<number>` keyword in the PR body automatically links and closes the issue when merged.
