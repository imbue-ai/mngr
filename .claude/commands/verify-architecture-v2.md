---
description: Assess whether the approach taken on a branch is the right way to solve the problem.
---

# Architecture Verification

Assess whether the approach taken on this branch is the right way to solve its problem. Specifically: does it fit existing codebase patterns and information flow, does it introduce unnecessary coupling or implicit dependencies, and is there a better alternative?

## Phase 1: Summarize the Problem

If you do not already know what the changes on this branch are supposed to accomplish, STOP and ask the user before continuing.

Write a CONCISE description of the problem the branch is trying to solve, based on your knowledge of the work done so far. Focus on the goal, not the implementation: what should work differently afterward, what is currently broken, or what structural problem exists in the code. Do not describe the approach the branch takes.

## Phase 2: Validate the Diff

Determine the base branch: use `$GIT_BASE_BRANCH` if set, otherwise default to `main`.

Spawn a Task subagent (`subagent_type: "general-purpose"`, `model: "haiku"`) to do a quick sanity check on the diff. Give it the problem description from Phase 1 and instruct it to skim (not analyze in depth -- a later step does that):

> The branch is supposed to: {problem description}
>
> Skim this diff and answer three questions:
> 1. Is the diff empty?
> 2. Does it include significant unrelated changes (e.g. from merged-in feature branches)? Ignore minor cleanups or small incidental fixes -- only flag changes that look like a separate logical effort. If so, describe what seems unrelated.
> 3. At a glance, does the scope of the changes look roughly complete for the stated goal, or does it look like only a partial solution or a work in progress?
>
> ```
> git diff ${base}...HEAD
> ```
>
> Keep your answer brief -- a detailed review happens later.

Based on the subagent's response:
- If the diff is empty, STOP and ask the user whether the work has been committed yet or whether the base branch is wrong.
- If it reports significant unrelated changes, STOP and explain to the user that this skill can only verify one logical change at a time. Ask which change they want to focus on (e.g. the main goal of the branch vs. an incidental fix). Then when spawning the analysis subagent in Phase 4, explicitly tell it to ignore the changes that are not part of the chosen focus.
- If it reports the work looks incomplete, flag that to the user and ask whether to proceed anyway.

## Phase 3: Prepare a Worktree

Resolve both commit hashes now, before spawning anything:

```bash
base_hash=$(git rev-parse {base_branch})
tip_hash=$(git rev-parse HEAD)
```

Create a temporary worktree with a unique name so the analysis subagent can read the pre-change codebase:

```bash
worktree_path=".worktree/arch-verify-$(head -c 8 /dev/urandom | xxd -p)"
git worktree add --detach $worktree_path $base_hash
```

## Phase 4: Spawn Analysis Subagent

Read the subagent prompt from [analyze-architecture.md](analyze-architecture.md). Spawn a single Task subagent (`subagent_type: "general-purpose"`, leaving model as default) with that prompt, prepending:
- The problem description from Phase 1
- The base commit hash ($base_hash) and feature branch tip hash ($tip_hash)
- The worktree path ($worktree_path)

## Phase 5: Cleanup and Report

Remove the temporary worktree:

```bash
git worktree remove $worktree_path
```

Relay the subagent's findings to the user. Report every point from the fit, unexpected choices, and verdict sections. Don't reproduce the structural footprint section on its own -- the user already knows what they built -- but reference specific details from it where needed to make the other points clear.
