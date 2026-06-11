- Add a daily `schedule:` trigger to the `minds launch-to-first-message`
  workflow. At 14:00 UTC (07:00 PDT / 06:00 PST) it builds + verifies the
  current mngr `main` HEAD against FCT `main`, with the full slack flow
  (latchkey + mocked slack server). Surfaces drift between the two repos
  the morning it happens instead of waiting for the next manual dispatch.
- `commit_sha` and `template_ref` inputs are now optional. Empty
  `commit_sha` -> `github.sha` (mngr main HEAD when triggered by schedule;
  caller's branch HEAD when dispatched without a value). Empty
  `template_ref` -> `main`. Existing dispatches that pass both inputs
  behave identically.
- The cron only fires once this workflow file lands on the default branch
  (`main`); GitHub Actions ignores schedule triggers defined only on
  feature branches.
