Added the `identify-bad-tests` Claude skill. It scans a target path -- either a whole library or any
subdirectory within one -- for low-quality, fragile, or misleading tests and reports candidates ranked
by importance into the containing library's `_tasks/bad-tests/<date>.md`, in the same format as the
other `identify-*` skills (so findings feed into `create-fixmes`). The skill grounds its checks in the
"# Testing" section of the style guide: tautological/unfalsifiable assertions, "no exception raised"
checks, tests coupled to implementation details, error tests that don't pin the error type/message,
weak coverage-chasing assertions, missing edge/branch cases, mock and fake misuse, flakiness and
isolation hazards, wrong test type/location/marking, test-grouping classes and poor naming, and
snapshot misuse. The central evaluation question is whether a test would actually fail if the code
under test had a real bug. Unlike the other skills it deliberately reads the `_test.py` / `test_*.py`
files (which the repo conventions normally skip), and it defers raw pattern occurrences already
counted by `test_ratchets.py` to those ratchets, reporting only the semantic test-quality problem.

Also fixed a contradictory instruction shared by the existing `identify-*` skills
(`identify-style-issues`, `identify-doc-code-disagreements`, `identify-outdated-docstrings`,
`identify-inconsistencies`, `identify-suspicious-edge-cases`): their intro said to commit when
finished, but their output files are gitignored and the closing line says no commit is needed.
Removed the contradictory parenthetical from each.

No runtime or tooling change.
