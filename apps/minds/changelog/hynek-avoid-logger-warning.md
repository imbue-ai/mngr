Stopped using `logger.warning()` throughout the minds backend. Every former warning is now classified as either a genuine error (logged with `logger.error()` -- or `logger.opt(exception=exc).error()` to preserve the traceback -- which is automatically reported to Sentry) or expected, business-as-usual behavior (logged with `logger.info()`).

This makes the logs honest about which conditions actually need attention: parse/protocol failures of our own data, failed cleanup that leaks resources, callback bugs, and failed user-initiated operations now surface as errors in Sentry, while graceful degradations (connector/auth backend unreachable, best-effort cleanup, optional fallbacks, malformed external input) stay at info.

Added a project-local ratchet test (`test_prevent_logger_warning`) that prevents new `logger.warning()` calls from being introduced in the shipped minds package.
