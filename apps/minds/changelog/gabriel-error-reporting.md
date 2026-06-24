Error reporting is now controlled by the user instead of environment variables.

On first launch (and once after upgrading), Minds shows a consent screen ahead of welcome/login that explains error reporting and lets you opt into "Report unexpected errors" and "Include logs" (both default off; "Include logs" only appears once reporting is on). The same two toggles live permanently under account settings and take effect immediately, with no restart.

Sentry now always initializes, but what it sends is gated live by these settings: with reporting off, automatic errors are never sent; with reporting on but logs off, errors are sent without log/traceback attachments. Manual bug reports (a future, explicit user action) are always sent regardless. The `MINDS_SENTRY_ENABLED` and `MINDS_SENTRY_S3_UPLOADS` environment variables no longer gate any of this.
