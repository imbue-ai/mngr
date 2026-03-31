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

Read it carefully.

## 2. Reproduce and investigate (in parallel)

First, gather context for the relevant library (per the "How to get started" instructions in CLAUDE.md).

Then launch two efforts concurrently:

- **Reproduce**: Ideally, write a regression test that fails before the fix and will pass after. If a test isn't practical, use a script or manual steps. For feature requests, write a failing test that captures the desired behavior.
- **Root-cause search**: Read the relevant code, trace the control flow, and identify where the fix should go.

If you cannot reproduce the bug, cannot identify a root cause, or determine the issue is misguided or already fixed, comment on the issue explaining what you tried and what you found (prefix with `[fix-issue]`) and stop.

## 3. Decide on approach

If there is a clear, unambiguous fix, just do it.

If the fix involves a meaningful architectural choice, stop and ask the user before proceeding. Briefly lay out the options and your recommendation.

## 4. Implement the fix

- Fix the root cause.
- Add or update tests to cover the fix.
- Get all tests passing (`uv run pytest` in the relevant project directory).

## 5. Commit and open a PR

Commit your changes, then open a PR with `Closes #<issue_number>` in the body so it auto-closes the issue on merge.

Note: CLAUDE.md says not to create PRs yourself. Ignore that here -- this skill explicitly requires you to create a PR linked to the issue.
