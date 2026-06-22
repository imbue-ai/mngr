Begin porting the Sentry error-reporting setup into the minds backend. `minds run` now calls `setup_sentry()` during startup (after logging is configured) so the Python backend reports errors and attaches logs to Sentry. The environment, release id, and git sha are placeholders for now and will be wired up to real values in a follow-up.

Enable the Sentry Flask integration so reported errors from web backend endpoints carry request context (transaction name, URL, method, query string, headers).
