Added the `identify-suspicious-edge-cases` Claude skill. It scans a target path -- either a whole
library or any subdirectory within one -- for suspicious edge-case handling (over-broad `except`
clauses, fallback `else` branches, defensive guards, fallback default values, and `Something | None`
optionals) and reports candidates ranked by importance into the containing library's
`_tasks/suspicious-edge-cases/<date>.md`, in the same format as the other `identify-*` skills (so
findings feed into `create-fixmes`). It is meant as an intermittent cleanup pass to
counteract over-defensive error handling: the default stance is suspicion, it errs toward
over-reporting, and the remedy for a correct-but-flagged handler is to add a clarifying comment
explaining why the logic is intentional. No runtime or tooling change.
