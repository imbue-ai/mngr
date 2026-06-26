`mngr latchkey forward` (the long-running gateway/reverse-tunnel supervisor daemon) now reports errors to Sentry, using the shared error-reporting machinery in `imbue_common`. It reports to whichever Sentry project the embedder points it at, tagged with the `mngr-latchkey-forward` service name so its events are distinguishable, and attaches its own structured (`events.jsonl`) and raw (`latchkey_forward.log`) logs.

Reporting is off by default and configured entirely via `MNGR_LATCHKEY_SENTRY_*` environment variables (deliberately namespaced `MNGR_LATCHKEY_*`, not `LATCHKEY_*`, to distinguish `mngr latchkey` from the upstream core `latchkey` project). The daemon owns no Sentry project / environment definitions of its own -- it receives concrete values as strings. The (mostly static) infrastructure is snapshotted into its environment at spawn:

- `MNGR_LATCHKEY_SENTRY_DSN` -- the Sentry DSN to report to.

- `MNGR_LATCHKEY_SENTRY_ENVIRONMENT` -- the Sentry environment label.

- `MNGR_LATCHKEY_SENTRY_RELEASE` and `MNGR_LATCHKEY_SENTRY_GIT_SHA` -- the release version and git SHA events are tagged with.

- `MNGR_LATCHKEY_SENTRY_S3_BUCKET` -- the S3 bucket for log/traceback attachments; empty means there is no bucket, so nothing is uploaded.

Sentry initializes whenever `DSN`, `ENVIRONMENT`, `RELEASE`, and `GIT_SHA` are all present (the daemon has no fallback of its own; run standalone without them it does nothing).

Whether the daemon actually *sends* reports (and attaches logs) is gated by a live consent file at `MNGR_LATCHKEY_SENTRY_CONSENT_FILE`, read on every event. This lets the embedder toggle consent on a running daemon -- a grant/revoke takes effect immediately, with no respawn -- exactly mirroring how the minds backend gates its own Sentry on live user settings.

When the minds desktop client spawns the daemon, it publishes the infrastructure variables automatically (resolving the DSN / environment / bucket from its own Sentry settings) and maintains the consent file from the user's error-reporting settings, rewriting it whenever the user changes consent.
