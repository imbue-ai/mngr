# Task 99.2: Launch the Review agent

## Goal

Spawn `/sculptor-workflow:review` in a new agent tab so the Review agent can
verify requirements coverage, re-run the test suite, and invoke the repo's
code-review skill. This is the final task in the plan.

## Background

This is the last task. Every feature task is complete and committed; Task 99.1
confirmed all tests pass. The Review agent reads the spec, plan, and the diff to
produce `review.md`.

Note the two-repo split this plan uses:

- **Spec + plan** live in the monorepo at `specs/window-error-watcher/`, on
  branch `preston/error-checker`.
- **Implementation** lives in the FCT clone at
  `.external_worktrees/forever-claude-template/`, on branch
  `preston/error-checker` (a separate git repo; gitignored by the monorepo).

The Review agent must look at BOTH diffs. Make this explicit in the seed.

## Files to modify/create

None. This task spawns an agent; it does not edit code.

## Implementation details

1. Compute the diff ranges:
   - Monorepo (spec + plan): `origin/main...HEAD`.
   - FCT implementation: inside `.external_worktrees/forever-claude-template/`,
     `origin/main...HEAD` (its default branch is also `main`).
2. Spawn a new agent in the same workspace via the `/sculptor:sculpt-cli` skill,
   invoking `/sculptor-workflow:review` there. Seed it with:
   - `Slug:` window-error-watcher
   - `Spec path:` specs/window-error-watcher/spec.md
   - `Plan folder:` specs/window-error-watcher/plan/
   - `Diff range:` origin/main...HEAD (monorepo: spec + plan)
   - `Implementation repo:` .external_worktrees/forever-claude-template/ (branch
     preston/error-checker) — review this clone's `origin/main...HEAD` diff for
     the actual `libs/error_watcher/` + `services.toml` + changelog changes.
   - `Note:` there is no `.sculptor/architecture.md` — this feature went spec →
     plan directly. Test commands: `cd libs/error_watcher && uv run pytest` and
     `cd libs/bootstrap && uv run pytest`, run from the FCT clone. Real-tmux E2E
     is intentionally manual per the FCT CLAUDE.md, so do not expect a real-tmux
     pytest; the `run_one_poll` integration test is the automated E2E layer.
3. The Review agent self-renames on entry; you do not need to rename it.
4. End this turn with **text instructions** pointing the user to the new Review
   tab. Do NOT call `mcp__sculptor__ask_user_question` (the workspace's "waiting
   for input" state must belong to the Review agent now).

## Verification checklist

- [ ] The Review agent is running in a new tab.
- [ ] The seed names both diffs (monorepo spec/plan and the FCT implementation
  clone).
- [ ] Text instructions point the user there.

## Commit policy

**Do NOT commit.** This task does not edit any files. After spawning the Review
agent, report success with no commit.
