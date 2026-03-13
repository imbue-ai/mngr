---
name: analyze-architecture
description: Analyze whether the approach taken on a branch fits existing codebase patterns.
---

# Architecture Analysis

You are analyzing whether the approach taken on a feature branch is the right way to solve its stated problem. You have been given:
- A **problem description** (what the branch is trying to accomplish)
- A **base commit hash** and **tip commit hash** (for diffing)

To read files as they were before the changes, use `git show {base}:path/to/file`. To read current (post-change) files, use the Read tool normally.

Perform these steps in order.

## Step 1: Understand the existing codebase

Build a thorough understanding of the code before looking at any changes. Read (using `git show {base}:path`):
- Project instructions and conventions: CLAUDE.md, style_guide.md, AGENTS.md
- Design and architecture docs
- The parts of the codebase that are relevant to the stated problem -- the files and modules you would expect to touch if you were implementing a solution yourself

The goal is to understand not just what the code does, but how the codebase is organized: what patterns it uses, how modules relate to each other, and where the boundaries are.

## Step 2: Generate independent approaches

Before looking at the actual changes, think of at least 3 ways you would solve the stated problem. For each, write one or two paragraphs covering the strategy, its tradeoffs, and which existing codebase patterns it leverages. This establishes an unbiased baseline for evaluating the actual implementation later.

## Step 3: Study the actual changes

Now read the diff (`git diff {base}...{tip}`) and the modified files on the feature branch in detail.

## Step 4: Characterize the structural footprint

Describe what the changes add to the codebase at a structural level:
- New functions, classes, modules, or external dependencies
- How data flows through the new code and connects to existing data flows
- Any new coupling between previously independent parts of the codebase (new imports, shared state, cross-module calls)
- Any new reliance on side information: environment variables, files on disk, global/mutable state, wall-clock time, process-level state, or anything else that is not passed in as an explicit argument. This is especially important to flag.

## Step 5: Evaluate fit with existing codebase

Judge whether the changes feel like they belong in this codebase:
- Do they follow the same patterns used for similar functionality elsewhere?
- Is there existing code they could have extended or reused instead of building something new?
- Where they diverge from established patterns, note it explicitly -- even if the divergence seems justified.

## Step 6: Compare against your independent approaches

Now compare the actual implementation to the approaches you proposed in Step 2:
- Which of your approaches does it most resemble, and how closely?
- Does it do anything you would not have predicted? Flag anything unexpected, even if it turns out to be well-motivated.
- Does it address the root cause of the problem, or work around it? Does it fully solve the stated goal, or only part of it?

## Step 7: Verdict

State whether you think this is the right approach. If you think there is a meaningfully better alternative -- one that fits the codebase more naturally, avoids unnecessary side information, or maintains cleaner boundaries -- describe it concretely.

## Step 8: Report

Return a structured report:
- **Structural footprint** -- what the changes add and how data flows through them (Step 4)
- **Fit with existing code** -- where the changes follow or break from established patterns (Step 5)
- **Unexpected choices** -- anything surprising relative to your independent approaches (Step 6)
- **Verdict** -- overall judgment and any concrete alternatives (Step 7)
