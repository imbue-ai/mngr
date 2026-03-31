---
name: verify-architecture
description: Assess whether the approach taken on a branch is the right way to solve the problem.
allowed-tools: Bash(git rev-parse *), Bash(git diff *), Bash(git log *), Bash(git show *), Bash(git ls-tree *), Bash(ls *), Bash(find *), Bash(grep *), Bash(echo "${GIT_BASE_BRANCH:-main}"), Bash(date -u +%Y-%m-%dT%H:%M:%SZ), Read, Write, Agent, AskUserQuestion
---

# Architecture Verification

Assess whether the approach taken on this branch is the right way to solve its problem. Specifically: does it fit existing codebase patterns and information flow, does it introduce unnecessary coupling or implicit dependencies, and is there a better alternative?

## Phase 1: Summarize the Problem

If you do not already know what the changes on this branch are supposed to accomplish, STOP and ask the user before continuing.

Write a CONCISE description of the problem the branch is trying to solve, based on your knowledge of the work done so far. This description must contain ONLY the problem -- not any part of the solution. Describe what should work differently afterward, what is currently broken, or what structural problem exists in the code. Do not mention any mechanism, technique, data structure, or approach used to fix it. The analysis agent needs to evaluate the approach independently, so any hint about the implementation will bias its judgment.

## Context

- Default base branch: !`echo "${GIT_BASE_BRANCH:-main}"` (use this unless the user specified a different one)
- Current HEAD: !`git rev-parse HEAD`

## Phase 2: Validate the Diff

Spawn a `validate-diff` Agent. Provide the base branch name and the problem description from Phase 1.

Based on the agent's response:
- If the diff is empty, STOP and ask the user whether the work has been committed yet or whether the base branch is wrong.
- If it reports significant unrelated changes, you MUST stop and consult the user -- do not dismiss this or proceed on your own. Unrelated changes in the diff will cause the analysis agent to waste effort on irrelevant code and produce worse results. Explain that this skill can only verify one logical change at a time. Ask which change they want to focus on (e.g. the main goal of the branch vs. an incidental fix). Then when spawning the analysis agent in Phase 3, explicitly tell it to ignore the changes that are not part of the chosen focus.
- If it reports the work looks incomplete, flag that to the user and ask whether to proceed anyway.

## Phase 3: Spawn Analysis Agent

Resolve the base branch commit hash:

```bash
git rev-parse {base_branch}
```

Spawn a single `analyze-architecture` Agent. Provide:
- The problem description from Phase 1
- The base commit hash and feature branch tip hash

## Phase 4: Report

Relay the agent's findings to the user. Report every point from the fit, unexpected choices, and verdict sections. Don't reproduce the structural footprint section on its own -- the user already knows what they built -- but reference specific details from it where needed to make the other points clear.

## Phase 5: Create Verification Marker

After reporting, create the verification marker so the stop hook knows architecture has been verified for this branch. Get the current branch name and timestamp:

```bash
git rev-parse --abbrev-ref HEAD
date -u +%Y-%m-%dT%H:%M:%SZ
```

Replace any `/` in the branch name with `_` (e.g., `mngr/my-feature` becomes `mngr_my-feature`). Then use the Write tool (without checking if the directory exists) to create `.reviewer/outputs/architecture/{sanitized_branch_name}.md` with the content `Verified at {timestamp}`.

## Important: when to re-run

Architecture verification is per-branch, not per-commit. You do NOT need to re-run it after every commit. However, if you later make changes that fundamentally alter the approach (new abstractions, changed data flow, different module boundaries), you should run /verify-architecture again to confirm the new direction is sound.
