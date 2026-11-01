- The app-release runbook (`docs/deploy/ops/app-release.md`) records that a
  channel publish does *not* start a smoke run, and gives the command to
  dispatch one by hand against the build a promotion just published.

- The services runbook (`docs/deploy/ops/services.md`) records that a
  successful production deploy dispatches the Imbue Studio smoke run, where to
  find the run, and what `--skip-smoke` does.
