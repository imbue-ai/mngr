The TMR GitHub Actions workflow (`.github/workflows/tmr.yml`) now uses
the canonical `--format` flag (the previous `--output-format` was not a
real option) and accepts three new optional `workflow_dispatch` inputs:
`modal_token_id`, `modal_token_secret`, and `mngr_user_id`. Supplying
your own Modal credentials and user ID lets you observe and debug the
modal agents launched by a CI run from your local `mngr list`. The
user-supplied Modal token values are masked from step logs via
`::add-mask::`, but they remain visible on the workflow run details
page to anyone with repo read access.
