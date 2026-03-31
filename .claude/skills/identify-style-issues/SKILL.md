---
name: identify-style-issues
argument-hint: [library_name]
description: Identify divergences from the style guide in the $1 library
---

Go gather all the context for the $1 library (per instructions in CLAUDE.md). Be sure to read non_issues.md as well.

Once you've gathered that context, please do the below (and commit when you're finished).

Your task is to identify any places where the $1 library diverges from the established style guide (style_guide.md).

Focus on the higher-level aspects of the style guide, such as code structure, organization, and design patterns (worry less about anything that should be caught by an automated linter or a ratchet).

In fact, for this reason it is important to go look at the existing ratchet tests--do NOT mention anything that is already covered by those tests.

If there are inconsistencies within the style guide itself (or aspects that it leaves ambiguous), please note those as well.

Do NOT report issues that are already covered by an existing FIXME

Do NOT report issues that are highlighted as non-issues in non_issues.md

After reviewing all the code in the library, think carefully about the most important stylistic inconsistencies and issues.

Then put them, in order from most important to least important, into a markdown file in the library's "_tasks/style/" folder (make one if you have to)  Name the file "<date>.md` (where you should get "date" by calling this precise command: "date +%Y-%m-%d-%T | tr : -")

For the format of the file, use the following:

```markdown
# Style issues in the $1 library (identified on <date>)
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
