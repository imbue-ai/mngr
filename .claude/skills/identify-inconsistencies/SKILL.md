---
name: identify-inconsistencies
argument-hint: [library_name]
description: Identify inconsistencies in the $1 library
---

Go gather all the context for the $1 library (per instructions in CLAUDE.md). Be sure to read non_issues.md as well.

Once you've gathered that context, please do the below (and commit when you're finished).

Your task is to identify inconsistencies in the $1 library.

In particular, focus on the code, and look for things that are done in different ways in different places, inconsistent variable/function/class naming, and any other code-level inconsistencies.

Do NOT worry about docstrings, comments, or documentation--focus only on the code itself (those will be covered by another task).

Do NOT worry about inconsistencies between the docs/specs and the code either (those will also be covered by another task).

Do NOT report issues that are already covered by an existing FIXME

Do NOT report issues that are highlighted as non-issues in non_issues.md

After reviewing all the code in the library, think carefully about the most important inconsistencies.

Then put them, in order from most important to least important, into a markdown file in the library's "_tasks/inconsistencies/" folder (make one if you have to)  Name the file "<date>.md` (where you should get "date" by calling this precise command: "date +%Y-%m-%d-%T | tr : -")

For the format of the file, use the following:

```markdown
# Inconsistencies in the $1 library (identified on <date>)
## 1. <Short description of inconsistency>

Description: <detailed description of the inconsistency, including file names and line numbers where applicable>

Recommendation: <your recommendation for how to fix the inconsistency>

Decision: Accept

## 2. <Short description of inconsistency>

Description: <detailed description of the inconsistency, including file names and line numbers where applicable>

Recommendation: <your recommendation for how to fix the inconsistency>

Decision: Accept

...
```

There's no need to commit when you're done (these files are gitignored). Just be sure to create the file in the right location with the right content.
