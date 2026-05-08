The TMR GitHub Actions workflow (`.github/workflows/tmr.yml`) now uses
the canonical `--format` flag (the previous `--output-format` was not a
real option) and accepts three new optional `workflow_dispatch` inputs:
`modal_token_id`, `modal_token_secret`, and `mngr_user_id`. Supplying
your own Modal credentials and user ID lets you observe and debug the
modal agents launched by a CI run from your local `mngr list`:
`MNGR_USER_ID` is exported into the orchestrator's process env so the
`mngr tmr` run itself attributes the agents it creates. A user-supplied
`modal_token_secret` is masked from step logs via `::add-mask::`, but
all `workflow_dispatch` input values remain visible on the workflow run
details page to anyone with repo read access.
