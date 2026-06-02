`UncappedClaudeError` (the plugin's base error) now inherits from `MngrError` instead of
`BaseMngrError`, matching the repo-wide consolidation of the error hierarchy under a single
user-facing parent class. This also removes a prior inconsistency where its subclasses
(`UnsupportedClaudeFlagError`, `InvalidStreamJsonInputError`, `MissingPromptError`) were already
`MngrError` instances via `UserInputError` while the base was not. No behavior change.
