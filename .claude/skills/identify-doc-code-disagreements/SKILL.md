---
name: identify-doc-code-disagreements
argument-hint: [library_name]
description: Identify places in the $1 library where the docs and code disagree
---

Go gather all the context for the $1 library (per instructions in CLAUDE.md). Be sure to read non_issues.md as well.

Once you've gathered that context, please do the below (and commit when you're finished).

Your task is to identify disagreements between the implementation and the documentation in the $1 library.

In particular, focus on logical, meaningful conflicts between what is said in any written documentation and what is actually implemented in the code.

Do NOT worry about functionality that is still clearly in-progress, under construction, etc--if something simply has not yet been implemented, that's ok. At most, you can suggest that the code be better about raising a NotImplementedError in such cases.

We want to focus on issues where something actually *is* implemented, but it's not implemented *how* the docs say it should be.

Do NOT worry other disagreements between really long, claude-generated "spec" files and the code (those are usually just left-over construction artifacts). If anything, you can simply highlight places where there was a big detailed spec that should have been deleted.

Do NOT worry other types of issues besides conflicts between the docs and code.

Do NOT report issues that are already covered by an existing FIXME

Do NOT report issues that are highlighted as non-issues in non_issues.md

After reviewing all the code in the library, think carefully about the most important disagreements between the docs and code.

Then put them, in order from most important to least important, into a markdown file in the library's "_tasks/docs/" folder (make one if you have to)  Name the file "<date>.md` (where you should get "date" by calling this precise command: "date +%Y-%m-%d-%T | tr : -")

For the format of the file, use the following:

```markdown
# Doc and code disagreements in the $1 library (identified on <date>)
## 1. <Short description of disagreement>

Description: <detailed description of the disagreement, including file names and line numbers where applicable>

Recommendation: <your recommendation for how to fix the disagreement>

Decision: Accept

## 2. <Short description of disagreement>

Description: <detailed description of the disagreement, including file names and line numbers where applicable>

Recommendation: <your recommendation for how to fix the disagreement>

Decision: Accept

...
```

There's no need to commit when you're done (these files are gitignored). Just be sure to create the file in the right location with the right content.
