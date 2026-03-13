---
description: Automatically find and fix code issues in the current branch. Iteratively verifies, plans fixes, and implements them with separate commits. Defers all review to the end.
allowed-tools: Bash(git status *), Bash(git rev-parse *), Bash(git log *), Bash(git revert *), Bash(date -u +%Y-%m-%dT%H:%M:%SZ), Bash(echo "${GIT_BASE_BRANCH:-main}"), Read, Write, Agent, AskUserQuestion
---

# Autofix

Iteratively verify the current branch for code issues, plan and implement fixes (each in a separate commit), and repeat until clean. At the end, present each fix for user review and revert any the user does not want.

## Instructions

### Phase 1: Setup

Autofix requires a clean git state. Before proceeding, check for uncommitted changes:

```bash
git status --porcelain
```

If there are any untracked, staged, or unstaged changes, commit them first (or add them to .gitignore if they should not be tracked). Do NOT proceed until `git status --porcelain` produces no output.

- Initial HEAD (`initial_head`): !`git rev-parse HEAD`
- Base branch (`base_branch`): !`echo "${GIT_BASE_BRANCH:-main}"`

If you do not already know what the changes on this branch are supposed to accomplish, STOP and ask the user before continuing.

Write a brief description of what the branch is trying to do. This helps the diff validation and fix agents distinguish intentional changes from issues.

### Phase 2: Validate the Diff

Spawn an Agent (`subagent_type: "general-purpose"`) and tell it to read `.claude/agents/validate-diff.md` and follow its instructions. Provide the base branch name and the problem description.

Based on the agent's response:
- If the diff is empty, STOP and ask the user whether the work has been committed yet or whether the base branch is wrong.
- If it reports significant unrelated changes, STOP and ask the user what the correct base branch is.
- If it reports the work looks incomplete, note this but proceed -- autofix works on whatever is there.

### Phase 3: Fix Loop

Repeat up to 10 times:

1. Record the current HEAD as `pre_iteration_head`.
2. Spawn a single Agent (`subagent_type: "general-purpose"`) and tell it the base branch (`{base_branch}`), the current HEAD hash (`{pre_iteration_head}`), and to read and follow `.claude/agents/verify-and-fix.md`.
3. Check if HEAD moved: compare `git rev-parse HEAD` to `pre_iteration_head`.
4. If HEAD did not move, no fixes were made. The branch is clean (or remaining issues are unfixable). Stop looping.
5. If HEAD moved, continue to the next iteration.

Important:
- Do NOT explore code, plan, or fix anything yourself. The agent does all the work.
- Each iteration gets a fresh-context agent, which is the whole point.
- Do NOT pass the agent any information about previous iterations or previous fixes. It operates from a clean slate every time.

### Phase 4: Review

After the loop ends:

1. Collect all fix commits: `git log --reverse --format="%H %s" {initial_head}..HEAD`
2. If there are no new commits, skip to step 8.
3. Check if `.autofix/config/auto-accept.md` exists. If it does, read it. This file contains free-text rules describing which kinds of fixes should be automatically accepted without prompting the user (e.g. "accept all naming fixes", "auto-accept anything in test files").
4. For each commit, check its full commit message against the auto-accept rules. If a commit matches, keep it automatically -- do not ask the user about it.
5. Ask about the remaining commits in a single `AskUserQuestion` call. Use one question per commit (up to 4 per call; if there are more than 4 commits, use multiple calls but still gather all answers before doing any git operations). Each question should:
   - Show the full commit message (which contains the problem and the fix).
   - Options: "Keep" and "Revert"
6. Only after ALL answers have been collected, revert the rejected commits. Run `git revert --no-edit {hash}` for each, in reverse chronological order (newest first) to avoid conflicts.
7. Report the final summary: how many fixes kept (noting which were auto-accepted), how many reverted. Note how many total issues were identified (from `.autofix/issues/*.jsonl` files).

### Phase 5: Create Verification Marker

8. After all review and reverts are complete, create the verification marker so the stop hook knows this commit has been verified:

- Get the current HEAD hash: `git rev-parse HEAD`
- Get the current timestamp: `date -u +%Y-%m-%dT%H:%M:%SZ`
- Use the Write tool, without checking if the directory exists, to create `.autofix/plans/{hash}_verified.md` with content `Verified clean at {timestamp}`.
