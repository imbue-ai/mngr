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
> 2. Does it include unrelated changes (e.g. from merged-in feature branches)? If so, describe what seems unrelated.
> 3. At a glance, does the scope of the changes look roughly complete for the stated goal, or does it look like only a partial solution or a work in progress?
>
> ```
> git diff ${base}...HEAD
> ```
>
> Keep your answer brief -- a detailed review happens later.

Based on the subagent's response:
- If the diff is empty, STOP and ask the user whether the work has been committed yet or whether the base branch is wrong.
- If it reports unrelated changes, STOP and explain to the user that this skill can only verify one logical change at a time. Ask which change they want to focus on (e.g. the main goal of the branch vs. an incidental fix). Then when spawning the analysis subagent in Phase 4, explicitly tell it to ignore the changes that are not part of the chosen focus.
- If it reports the work looks incomplete, flag that to the user and ask whether to proceed anyway.

## Phase 3: Prepare a Worktree

Create a temporary worktree so the analysis subagent can read the pre-change codebase:

```bash
git worktree add --detach .worktree/arch-verify ${GIT_BASE_BRANCH:-main}
```

## Phase 4: Spawn Analysis Subagent

Spawn a single Task subagent (`subagent_type: "general-purpose"`) and give it:
- The problem description from Phase 1
- The base branch worktree path (`.worktree/arch-verify`)
- The feature branch tip hash (`git rev-parse HEAD`)
- The base branch name

The subagent prompt should instruct it to perform these steps in order:

### Step 1: Understand the existing codebase

Working in the base branch worktree, build a thorough understanding of the code before looking at any changes. Read:
- Project instructions and conventions: CLAUDE.md, style_guide.md, AGENTS.md
- Design and architecture docs (anything in docs/ describing system design)
- The files that were changed on the feature branch, plus their surrounding context (use `git diff --name-only {base}...{tip}` to identify them, then read the base-branch versions and neighboring files)

The goal is to understand not just what the code does, but how the codebase is organized: what patterns it uses, how modules relate to each other, and where the boundaries are.

### Step 2: Generate independent approaches

Before looking at the actual changes, think of at least 3 ways you would solve the stated problem. For each, write one or two paragraphs covering the strategy, its tradeoffs, and which existing codebase patterns it leverages. This establishes an unbiased baseline for evaluating the actual implementation later.

### Step 3: Study the actual changes

Now read the diff (`git diff {base}...{tip}`) and the modified files on the feature branch in detail.

### Step 4: Characterize the structural footprint

Describe what the changes add to the codebase at a structural level:
- New functions, classes, modules, or external dependencies
- How data flows through the new code and connects to existing data flows
- Any new coupling between previously independent parts of the codebase (new imports, shared state, cross-module calls)
- Any new reliance on side information: environment variables, files on disk, global/mutable state, wall-clock time, process-level state, or anything else that is not passed in as an explicit argument. This is especially important to flag.

### Step 5: Evaluate fit with existing codebase

Judge whether the changes feel like they belong in this codebase:
- Do they follow the same patterns used for similar functionality elsewhere?
- Is there existing code they could have extended or reused instead of building something new?
- Where they diverge from established patterns, note it explicitly -- even if the divergence seems justified.

### Step 6: Compare against your independent approaches

Now compare the actual implementation to the approaches you proposed in Step 2:
- Which of your approaches does it most resemble, and how closely?
- Does it do anything you would not have predicted? Flag anything unexpected, even if it turns out to be well-motivated.
- Does it address the root cause of the problem, or work around it? Does it fully solve the stated goal, or only part of it?

### Step 7: Verdict

State whether you think this is the right approach. If you think there is a meaningfully better alternative -- one that fits the codebase more naturally, avoids unnecessary side information, or maintains cleaner boundaries -- describe it concretely.

### Step 8: Report

Return a structured report:
- **Structural footprint** -- what the changes add and how data flows through them (Step 4)
- **Fit with existing code** -- where the changes follow or break from established patterns (Step 5)
- **Unexpected choices** -- anything surprising relative to your independent approaches (Step 6)
- **Verdict** -- overall judgment and any concrete alternatives (Step 7)

## Phase 5: Cleanup and Report

Remove the temporary worktree:

```bash
git worktree remove .worktree/arch-verify
```

Summarize the subagent's findings for the user. Focus on what matters most for deciding whether to keep or rethink the current approach:
- Where the implementation diverges from how the codebase normally does things
- Anything unexpected about the approach that deserves scrutiny
- The overall verdict, and any concrete alternatives worth considering
