TMR test scope is being re-anchored from the tutorial block to each test
function's docstring, so that TMR can run against all release tests rather than
only the e2e tutorial tests.

As part of this, the e2e test fixture now captures each test's docstring (the
verbatim tutorial block plus its crystallized scope) and the rendered test
detail pages show it under a "Docstring" heading. The legacy per-test
`tutorial_block.txt` artifact is still rendered (as "Tutorial block") for any
output produced before tests are migrated to carry the block in their docstring.
