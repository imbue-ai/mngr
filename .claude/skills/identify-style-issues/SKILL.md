---
name: identify-style-issues
argument-hint: [target_path]
description: Identify divergences from the style guide under the $1 path
---

The argument `$1` is the path to scan. It may be an entire library (e.g. `libs/mngr`, or just the bare name `mngr`) or any subdirectory within one (e.g. `libs/mngr/imbue/mngr/cli`), so you can scope this skill narrowly to part of a library when that is all you care about.

Before doing anything else, resolve these two things from `$1` and state them explicitly:

- **The scan scope**: the directory tree you will examine. This is `$1` itself, resolved to a real path. (If `$1` is a bare library name like `mngr`, resolve it to `libs/mngr` or `apps/mngr` -- whichever exists.) You must only report findings for code under this path.
- **The containing library**: the project directory that owns the scan scope. Projects always live at `libs/<name>` or `apps/<name>`, so the containing library is exactly that two-component prefix of the scan-scope path (e.g. for a scan scope of `libs/mngr/imbue/mngr/cli`, the containing library is `libs/mngr`; for a scan scope of `libs/mngr`, it is `libs/mngr` itself). You need this both to gather context and to know where to write the output file -- so make sure you have identified it unambiguously before continuing.

Go gather all the context for the containing library (per instructions in CLAUDE.md). Even when the scan scope is a small subdirectory, you still need the whole containing library's context (style guide, primitives, data_types, interfaces, utils) to evaluate the code in context. Be sure to read the containing library's non_issues.md as well.

Once you've gathered that context, please do the below.

Your task is to identify any places where the code within the scan scope diverges from the established style guide (style_guide.md).

Focus on the higher-level aspects of the style guide, such as code structure, organization, and design patterns (worry less about anything that should be caught by an automated linter or a ratchet).

In fact, for this reason it is important to go look at the existing ratchet tests--do NOT mention anything that is already covered by those tests.

If there are inconsistencies within the style guide itself (or aspects that it leaves ambiguous), please note those as well.

Do NOT report issues that are already covered by an existing FIXME

Do NOT report issues that are highlighted as non-issues in non_issues.md

After reviewing all the code in the scan scope, think carefully about the most important stylistic inconsistencies and issues.

Then put them, in order from most important to least important, into a markdown file in the containing library's "_tasks/style/" folder (make one if you have to, and always use the containing library's folder even when the scan scope was a subdirectory, so the findings live where create-fixmes and the other identify-* outputs expect them).  Name the file "<date>.md` (where you should get "date" by calling this precise command: "date +%Y-%m-%d-%T | tr : -")

For the format of the file, use the following:

```markdown
# Style issues under <scan scope> (identified on <date>)
## 1. <Short description of style issue>

Description: <detailed description of the style issue, including file names and line numbers where applicable>

Recommendation: <your recommendation for how to fix the style issue>

Decision: Accept

## 2. <Short description of style issue>

Description: <detailed description of the style issue, including file names and line numbers where applicable>

Recommendation: <your recommendation for how to fix the style issue>

Decision: Accept

...
```

There's no need to commit when you're done (these files are gitignored). Just be sure to create the file in the right location with the right content.
