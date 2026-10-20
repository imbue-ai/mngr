The repo-wide type-check gate (`test_no_type_errors`, backed by `check_no_type_errors`) now
reports what actually failed. It runs `ty check --output-format concise`, which prints one
line per diagnostic with a fixed grammar, parses that grammar into a `TypeCheckOutcome`, and
builds its failure message from the parsed facts.

Before, the gate counted errors by scanning ty's human-formatted output for lines starting
with `error[` or containing `error:`, then dumped the whole output into the assertion. Both
halves misreported. The substring search also matched source lines that ty echoes for
context, so a run whose real content was three `unknown-argument` errors was announced as
"6 error(s)" -- the extra three were one echoed parameter declaration, `transition_error: str
| None = None`, quoted once per error. And because ty sorts diagnostics by path, the dumped
output opened with whichever diagnostic sorted first, which in that run was a
`possibly-missing-submodule` *warning* from `apps/`. ty exits zero on a warning-only run, so
that warning could not have failed anything -- but it was the text that landed in the JUnit
`message` attribute and the CI check-run summary, and from there it was read as the cause and
filed as a flaky test (MIND-319) against a branch that simply had three type errors.

The message now opens with the exact error count and lists the error-severity diagnostics
first. Warnings follow under a heading that says ty exits zero on them, so they did not cause
the failure. Stdout that matches neither the diagnostic grammar nor ty's own summary line is
reported as unparsed rather than dropped, so a ty crash or an output-format change surfaces
instead of quietly shrinking the counts, and a non-zero exit with no diagnostics still fails
with its own headline. One `ty check` run per test, as before.

What fails the gate is unchanged: error-severity diagnostics, exactly as ty decides. Warnings
are shown but still do not fail CI. The switch to the concise format drops each diagnostic's
source snippet from the message; the message closes by naming `uv run ty check` as the way to
get it back. In exchange the whole diagnostic list now fits inside the 2000 characters CI
shows -- ten diagnostics came to 12.8 KB in the default format and 1.8 KB in this one.
