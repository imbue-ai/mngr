Added a `--timezone` option to `mngr schedule add` that pins the IANA timezone
in which the `--schedule` cron expression is interpreted (e.g.
`--timezone America/Los_Angeles`).

Previously the cron was always interpreted in the deploying machine's local
timezone, so the same schedule could fire at different wall-clock times
depending on where it was deployed from. Pinning `--timezone` makes the fire
time deterministic. The value is validated against the IANA timezone database
at deploy time. The option is only supported for the modal provider; passing it
with `--provider local` is an error.
