---
description: Assess whether the approach taken on a branch is the right way to solve the problem.
allowed-tools: Bash:*, Read, Agent, AskUserQuestion
---

# Architecture Verification

Assess whether the approach taken on this branch is the right way to solve its problem. Specifically: does it fit existing codebase patterns and information flow, does it introduce unnecessary coupling or implicit dependencies, and is there a better alternative?

## Phase 1: Summarize the Problem

If you do not already know what the changes on this branch are supposed to accomplish, STOP and ask the user before continuing.

Write a CONCISE description of the problem the branch is trying to solve, based on your knowledge of the work done so far. This description must contain ONLY the problem -- not any part of the solution. Describe what should work differently afterward, what is currently broken, or what structural problem exists in the code. Do not mention any mechanism, technique, data structure, or approach used to fix it. The analysis agent needs to evaluate the approach independently, so any hint about the implementation will bias its judgment.

## Phase 2: Validate the Diff

Determine the base branch: use `$GIT_BASE_BRANCH` if set, otherwise default to `main`.

Read the diff validation prompt from `.claude/skills/autofix/validate-diff.md`. Spawn an Agent (`subagent_type: "general-purpose"`, `model: "haiku"`) with that prompt, providing the base branch name and the problem description from Phase 1.

Based on the agent's response:
- If the diff is empty, STOP and ask the user whether the work has been committed yet or whether the base branch is wrong.
- If it reports significant unrelated changes, you MUST stop and consult the user -- do not dismiss this or proceed on your own. Unrelated changes in the diff will cause the analysis agent to waste effort on irrelevant code and produce worse results. Explain that this skill can only verify one logical change at a time. Ask which change they want to focus on (e.g. the main goal of the branch vs. an incidental fix). Then when spawning the analysis agent in Phase 4, explicitly tell it to ignore the changes that are not part of the chosen focus.
- If it reports the work looks incomplete, flag that to the user and ask whether to proceed anyway.

## Phase 3: Prepare a Worktree

Resolve both commit hashes now, before spawning anything:

```bash
base_hash=$(git rev-parse {base_branch})
tip_hash=$(git rev-parse HEAD)
```

Create a temporary worktree with a unique name so the analysis agent can read the pre-change codebase:

```bash
worktree_path="/tmp/arch-verify-$(head -c 8 /dev/urandom | xxd -p)"
git worktree add --detach $worktree_path $base_hash
```

## Phase 4: Spawn Analysis Agent

Read the agent prompt from `.claude/skills/verify-architecture/analyze-architecture.md`. Spawn a single Agent (`subagent_type: "general-purpose"`, leaving model as default) with that prompt, prepending:
- The problem description from Phase 1
- The base commit hash ($base_hash) and feature branch tip hash ($tip_hash)
- The worktree path ($worktree_path)

## Phase 5: Cleanup and Report

Remove the temporary worktree:

```bash
git worktree remove $worktree_path
```

Relay the agent's findings to the user. Report every point from the fit, unexpected choices, and verdict sections. Don't reproduce the structural footprint section on its own -- the user already knows what they built -- but reference specific details from it where needed to make the other points clear.
