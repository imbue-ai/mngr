Begin porting the Sentry error-reporting setup into the minds backend. `minds run` now calls `setup_sentry()` during startup (after logging is configured) so the Python backend reports errors and attaches logs to Sentry. The environment, release id, and git sha are placeholders for now and will be wired up to real values in a follow-up.

Enable the Sentry Flask integration so reported errors from web backend endpoints carry request context (transaction name, URL, method, query string, headers).

Add uploading of log files and traceback-with-locals attachments to S3 for Sentry error reports. The log-collection logic now matches the minds log layout: a flat logs directory (`~/.minds/logs`) containing the live Python backend JSONL log, its timestamp-suffixed rotated siblings, and the Electron log, all gzip-compressed on upload. Sentry's `log_folder` is now the minds logs directory (exposed as `WorkspacePaths.log_dir`) rather than the data directory.

Select the Sentry DSN and S3 bucket from the activated minds env (`minds env activate`): production reports to the production Sentry project and bucket, staging to the staging project and bucket, and every other env (dev-*, ci-*, or no activated env) to the dev Sentry project with no S3 uploads (so dev machines never ship potentially-sensitive attachments off-box).

Bump the bundled latchkey CLI to 2.17.2.
