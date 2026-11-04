- The smoke journey runs default-workspace-template's CI test suites inside the
  Imbue Cloud workspace it creates, right after creating it. Which suites run,
  and how, comes from the template's own `offload-modal*.toml` at the version
  the workspace was created from, retries included; the run goes through the
  app's own mngr on the app's sign-in.

- Every template test is a subtest of the journey labelled
  `dwt:<suite>::<node id>`, so each one that fails in the workspace is its own
  `SUBFAILED` result, and the journey carries on past it. Reports, logs and a
  `summary.md` land under `.test_output/smoke/<test name>/dwt/`, and each
  session writes `.test_output/smoke/verdict.json`, counting template failures
  apart from the rest.

- The suites get 75 minutes, and the journey's timeout grows from 60 to 145
  minutes.

- `MINDS_SMOKE_SKIP_DWT_TESTS=1` skips the template's tests in a run, reported
  as a skipped `Step 3b` subtest; unset or `0` runs them, and any other value
  errors the journey before the app launches.
