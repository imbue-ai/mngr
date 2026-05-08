The TMR GitHub Actions workflow (`.github/workflows/tmr.yml`) now uses
the canonical `--format` flag (the previous `--output-format` was not a
real option) and accepts a new optional `workflow_dispatch` input:
`mngr_user_id`. When set, it is exported into the orchestrator's
process env so the `mngr tmr` run attributes the modal agents it
creates to that user, with the goal of letting them be observed from
the user's local `mngr list`.
