`mngr latchkey forward` (the long-running gateway/reverse-tunnel supervisor daemon) now reports errors to Sentry, using the shared error-reporting machinery in `imbue_common`. It reports to whichever Sentry project the embedder points it at, tagged with the `mngr-latchkey-forward` service name so its events are distinguishable, and attaches its own structured (`events.jsonl`) and raw (`latchkey_forward.log`) logs.

Reporting is off by default and configured entirely via `MNGR_LATCHKEY_SENTRY_*` environment variables (deliberately namespaced `MNGR_LATCHKEY_*`, not `LATCHKEY_*`, to distinguish `mngr latchkey` from the upstream core `latchkey` project). The daemon owns no Sentry project / environment definitions of its own -- it receives concrete values as strings:

- `MNGR_LATCHKEY_SENTRY_ENABLED` -- opt-in switch.

- `MNGR_LATCHKEY_SENTRY_DSN` -- the Sentry DSN to report to.

- `MNGR_LATCHKEY_SENTRY_ENVIRONMENT` -- the Sentry environment label.

- `MNGR_LATCHKEY_SENTRY_RELEASE` and `MNGR_LATCHKEY_SENTRY_GIT_SHA` -- the release version and git SHA events are tagged with.

- `MNGR_LATCHKEY_SENTRY_S3_BUCKET` -- the S3 bucket for log/traceback attachments; empty means upload nothing.

`DSN`, `ENVIRONMENT`, `RELEASE`, and `GIT_SHA` are required when reporting is enabled (the daemon has no fallback of its own); if any is missing, Sentry setup is skipped with a warning rather than crashing the daemon.

When the minds desktop client spawns the daemon, it publishes these variables automatically -- resolving the DSN / environment / bucket from its own Sentry settings -- so the daemon inherits whether Sentry and log uploads are enabled.
