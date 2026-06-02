Updated to the repo-wide error-hierarchy consolidation: the check runner's
`except (BaseMngrError, OSError)` now reads `except (MngrError, OSError)` (`BaseMngrError` has
been removed). No behavior change.
