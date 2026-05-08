`mngr tmr` accepts a new repeatable flag `--additional-authorized-host`
that adds SSH public key lines to the `authorized_keys` file installed
on each agent host (test agents, host pool, snapshotter, and
integrator). This lets you SSH directly into any agent host TMR
creates, primarily for live debugging.

The TMR GitHub Actions workflow (`.github/workflows/tmr.yml`) now uses
the canonical `--format` flag (the previous `--output-format` was not a
real option) and accepts two new optional `workflow_dispatch` inputs:

- `mngr_user_id`: exported into the orchestrator's process env so the
  `mngr tmr` run attributes the modal agents it creates to that user,
  with the goal of letting them be observed from the user's local
  `mngr list`.
- `additional_authorized_hosts`: one SSH public key per line; each
  non-empty line is forwarded to `mngr tmr` as a separate
  `--additional-authorized-host` argument.
