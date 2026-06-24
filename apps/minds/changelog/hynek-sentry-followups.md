Hardened the Sentry error-reporting path for failures that happen *inside* Sentry event processing (the `before_send` hook, Sentry callbacks, and the HTTP transport).

Consolidated both legs of reporting such a failure into the `log_error_inside_sentry` helper: it now always records the failure in the local app log (so it is never lost) and reports it to Sentry via a minimal event. Previously only the `before_send` path logged locally, so failures in Sentry callbacks and the transport left nothing in the local log.

The local log line is marked so a new loguru filter on the Sentry event handler drops it, preventing it from becoming a second, separate Sentry event.

Made `log_error_inside_sentry` non-reentrant. Reporting goes through `capture_event`, which re-runs the whole `before_send` chain; if the failure being reported originates in `before_send` itself, that previously recursed until the stack was exhausted. The helper now drops nested calls so reporting is attempted at most once.
