TMR test scope is being re-anchored from the tutorial block to each test
function's docstring, so that TMR can run against all release tests rather than
only the e2e tutorial tests.

As part of this, the e2e test fixture now captures each test's docstring (the
verbatim tutorial block plus its crystallized scope) and the rendered test
detail pages show it under a "Docstring" heading.

All e2e tutorial tests have been migrated to the new format: each test's tutorial
block now lives in its docstring (under a `Tutorial block:` section) followed by a
`Scope:` section describing what the test verifies. The `e2e.write_tutorial_block`
helper has been removed now that no test uses it.
