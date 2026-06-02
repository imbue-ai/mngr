Added the `identify-suspicious-edge-cases` Claude skill. It scans a target path -- either a whole
library or any subdirectory within one -- for suspicious edge-case handling (over-broad `except`
clauses, fallback `else` branches, defensive guards, fallback default values, and `Something | None`
optionals) and reports candidates ranked by importance into the containing library's
`_tasks/suspicious-edge-cases/<date>.md`, in the same format as the other `identify-*` skills (so
findings feed into `create-fixmes`). It is meant as an intermittent cleanup pass to
counteract over-defensive error handling: the default stance is suspicion, it errs toward
over-reporting, and the remedy for a correct-but-flagged handler is to add a clarifying comment
explaining why the logic is intentional.

Also generalized the existing `identify-*` skills (`identify-inconsistencies`, `identify-style-issues`,
`identify-outdated-docstrings`, `identify-doc-code-disagreements`) to take a target path instead of a
library name. Each now accepts either a whole library or any subdirectory within one, deterministically
derives the containing library (the `libs/<name>` or `apps/<name>` prefix) for context-gathering, and
always writes its findings into that containing library's `_tasks/` folder. This lets the codebase-review
skills be scoped to part of a library, which matters now that libraries are much larger than when these
skills were written.

No runtime or tooling change.
