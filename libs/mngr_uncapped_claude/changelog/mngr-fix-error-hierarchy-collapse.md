Updated to the repo-wide error-hierarchy consolidation: `except BaseMngrError` handlers now use
`except MngrError` (`BaseMngrError` has been removed). No behavior change. The error-hierarchy
unit test (`errors_test.py`), which only documented the old two-tier distinction, was removed.
