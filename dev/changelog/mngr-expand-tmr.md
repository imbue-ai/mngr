`scripts/tutorial_matcher.py` now reads the tutorial block from each test
function's docstring (under a `Tutorial block:` section) instead of from a
`write_tutorial_block(...)` call, matching the new docstring-anchored scheme for
TMR. Added `specs/docstring-anchored-tmr.md` describing the overall change.
