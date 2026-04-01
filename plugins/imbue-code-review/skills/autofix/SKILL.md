---
name: autofix
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

Spawn a `validate-diff` Agent. Provide the base branch name and the problem description.

Based on the agent's response:
- If the diff is empty, STOP and ask the user whether the work has been committed yet or whether the base branch is wrong.
- If it reports changes that don't belong to this branch (i.e., changes you didn't make / the implementer agent wouldn't have made), STOP and ask the user which changes should be in scope. Downstream agents review the entire diff and will act on out-of-scope code -- regardless of how the extra changes got there (wrong base branch, stale local ref, a merge, or incidental edits). Ask the user for the correct base branch or which changes to focus on, then when spawning the verify-and-fix agent in Phase 3, explicitly tell it to ignore the out-of-scope changes.
- If it reports the work looks incomplete, note this but proceed -- autofix works on whatever is there.

### Phase 3: Fix Loop

Repeat up to 10 times:

1. Record the *full* current HEAD as `pre_iteration_head`.
2. Print a message telling the user that issues for this iteration will be saved to: `.reviewer/outputs/autofix/issues/{pre_iteration_head}.jsonl`
3. Spawn a single `verify-and-fix` Agent, providing the base branch (`{base_branch}`), the *full* current HEAD hash (`{pre_iteration_head}`), and the issue categories path: `.reviewer/code-issue-categories.md` if it exists, otherwise `${CLAUDE_PLUGIN_ROOT}/agents/categories/code-issue-categories.md`.
4. Wait for the agent to finish.
5. Check if HEAD moved: compare `git rev-parse HEAD` to `pre_iteration_head`.
6. If HEAD did not move, no fixes were made. The branch is clean (or remaining issues are unfixable). Stop looping.
7. If HEAD moved, continue to the next iteration.

Important:
- Do NOT explore code, plan, or fix anything yourself. The agent does all the work.
- Each iteration gets a fresh-context agent, which is the whole point.
- Do NOT pass the agent any information about previous iterations or previous fixes. It operates from a clean slate every time.
- You MUST explicitly wait for the `verify-and-fix` agent task to finish--do *not* simply finish your response!
- You MUST use your ability to wait for your own Task or Agent primitives in order to wait for the agent! Do not try to sleep or poll--that is inefficient and unreliable.
- Do *NOT* make any changes or run any tests or commands yourself while the above loop is running! That will be handled by the agent.

### Phase 4: Review

After the loop ends:

1. Collect all fix commits: `git log --reverse --format="%H %s" {initial_head}..HEAD`
2. If there are no new commits, skip to step 8.
3. Check if `.reviewer/autofix/auto-accept.md` exists. If it does, read it. This file contains free-text rules describing which kinds of fixes should be automatically accepted without prompting the user (e.g. "accept all naming fixes", "auto-accept anything in test files").
4. For each commit, check its full commit message against the auto-accept rules. If a commit matches, keep it automatically -- do not ask the user about it.
5. Ask about the remaining commits in a single `AskUserQuestion` call. Use one question per commit (up to 4 per call; if there are more than 4 commits, use multiple calls but still gather all answers before doing any git operations). Each question should:
   - Show the full commit message (which contains the problem and the fix).
   - Options: "Keep" and "Revert"
6. Only after ALL answers have been collected, revert the rejected commits. Run `git revert --no-edit {hash}` for each, in reverse chronological order (newest first) to avoid conflicts.
7. Report the final summary: how many fixes kept (noting which were auto-accepted), how many reverted. Note how many total issues were identified (from `.reviewer/outputs/autofix/issues/*.jsonl` files).

# RUN TIME OVERRIDE

For *this particular run* of the `autofix` command, follow these adjustments from the user to the normal process:

```
$ARGUMENTS
```
