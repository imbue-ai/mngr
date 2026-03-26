---
name: create-fixmes
argument-hint: [input_file]
description: Create FIXME's in the codebase for each of the issues in the given input file.
---

Your task is to create "# FIXME:" comments in the codebase for each of the issues listed in this file: $1

My decisions about what to do for each issue are in the "decision" section. If it says "ignore", then do nothing for that issue.

Make exactly ONE "# FIXME:" comment per issue (that I accepted). It should contain all of the necessary context about the issue and what to fix (both in the particular instance the FIXME is near, and whether other similar instances should be fixed too)

In order to figure out the best single place for the FIXME comment, first go gather all of the context for the relevant library (per instructions in CLAUDE.md).

Then think carefully about where the best place is to put the FIXME comment for each issue, and what exactly should be expressed in each issue.  If the FIXME applies to more than just this one instance, be clear about that in the description, but do NOT make multiple FIXME comments for the same issue.

After that, go ahead and insert the FIXME comments in the appropriate places in the codebase. Remember that multi-line descriptions should have their subsequent lines have TWO spaces (ie, "  ") after the "#" character, not just one

Once the comments have been added, commit.

Finally, move $1 to the "done" folder (the path ends in ".../_tasks/<something>/<date>.md", so just move it to ".../_tasks/done/<date>-<something>.md")
