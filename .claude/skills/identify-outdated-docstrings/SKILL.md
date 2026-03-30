---
name: identify-outdated-docstrings
argument-hint: [library_name]
description: Identify outdated docstrings in the $1 library
---

Go gather all the context for the $1 library (per instructions in CLAUDE.md). Be sure to read non_issues.md as well.

Once you've gathered that context, please do the below (and commit when you're finished).

Your task is to identify outdated docstrings in the $1 library.

Do NOT worry about any other type of outdated documentation, or any other types of issues beyond outdated docstrings (all those will also be covered by another task).

Do NOT report issues that are already covered by an existing FIXME

Do NOT report issues that are highlighted as non-issues in non_issues.md

After reviewing all the code in the library, think carefully about the most important outdated docstrings.

Then put them, in order from most important to least important, into a markdown file in the library's "_tasks/docstrings/" folder (make one if you have to)  Name the file "<date>.md` (where you should get "date" by calling this precise command: "date +%Y-%m-%d-%T | tr : -")

For the format of the file, use the following:

```markdown
# Outdated docstrings in the $1 library (identified on <date>)
## 1. <fully specified function/class/module name with the outdated docstring>

### Current:

<the current value of the docstring>

### Problem(s):

<short description of what is wrong with the docstring>

### Recommendation:

<your improved docstring>

### Decision:

Accept


## 2. <fully specified function/class/module name with the outdated docstring>

### Current:

<the current value of the docstring>

### Problem(s):

<short description of what is wrong with the docstring>

### Recommendation:

<your improved docstring>

### Decision:

Accept


...
```

There's no need to commit when you're done (these files are gitignored). Just be sure to create the file in the right location with the right content.
