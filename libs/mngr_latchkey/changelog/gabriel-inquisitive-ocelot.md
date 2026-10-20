The `mngr latchkey forward` daemon's Sentry consent file now carries a single flag.

`ForwardSentryConsent` (the JSON the embedder writes to `MNGR_LATCHKEY_SENTRY_CONSENT_FILE`) drops the `include_error_logs` field: the remaining `report_unexpected_errors` flag now gates both whether the daemon sends automatic reports and whether their log/traceback attachments are uploaded. An absent/unreadable consent file still means reporting is off.
