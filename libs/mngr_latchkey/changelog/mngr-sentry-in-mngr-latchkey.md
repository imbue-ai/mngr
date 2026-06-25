`mngr latchkey forward` (the long-running gateway/reverse-tunnel supervisor daemon) now reports errors to Sentry, using the shared error-reporting machinery in `imbue_common`. It reports to the same Sentry projects as the minds backend, tagged with the `mngr-latchkey-forward` service name so its events are distinguishable, and attaches its own structured (`events.jsonl`) and raw (`latchkey_forward.log`) logs.

Reporting is off by default and configured entirely via `MNGR_LATCHKEY_SENTRY_*` environment variables (deliberately namespaced `MNGR_LATCHKEY_*`, not `LATCHKEY_*`, to distinguish `mngr latchkey` from the upstream core `latchkey` project):

- `MNGR_LATCHKEY_SENTRY_ENABLED` -- opt-in switch.

- `MNGR_LATCHKEY_SENTRY_S3_UPLOADS` -- opt-in for uploading log/traceback attachments to S3.

- `MNGR_LATCHKEY_SENTRY_ENVIRONMENT` -- which Sentry project (`production`/`staging`/`development`).

- `MNGR_LATCHKEY_SENTRY_RELEASE` and `MNGR_LATCHKEY_SENTRY_GIT_SHA` -- the release version and git SHA the events are tagged with. These are required when reporting is enabled (the daemon has no fallback of its own); if any required variable is missing or invalid while enabled, Sentry setup is skipped with a warning rather than crashing the daemon.

When the minds desktop client spawns the daemon, it publishes these variables automatically, derived from its own Sentry settings, so the daemon inherits whether Sentry and S3 uploads are enabled (and the environment/release/git SHA) from minds.
