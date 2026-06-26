`scripts/tutorial_matcher.py` now reads the tutorial block from each test
function's docstring (under a `Tutorial block:` section) instead of from a
`write_tutorial_block(...)` call, matching the new docstring-anchored scheme for
TMR. Added `specs/docstring-anchored-tmr.md` describing the overall change.

The `sync-tutorial-to-e2e-tests` skill now emits the docstring format (verbatim
`Tutorial block:` section plus a `Scope:` section) and crystallizes the implicit
requirements of each block's commands into the scope, rather than calling
`write_tutorial_block`.

The TMR workflow (`.github/workflows/tmr.yml`, including the daily scheduled run)
now defaults to all of mngr's release tests (`libs/mngr` with
`-m "release and not docker and not docker_sdk"`) rather than only the e2e
tutorial subset. Docker-marked release tests are excluded because they need a
real Docker daemon and run on a GitHub runner in `release-tests.yml`, not on the
Modal hosts TMR provisions.
