Port the Sentry error-reporting setup into the minds backend. `minds run` now calls `setup_sentry()` during startup (after logging is configured) so the Python backend reports errors (and attaches logs) to Sentry.

Enable the Sentry Flask integration so reported errors from web backend endpoints carry request context (transaction name, URL, method, query string, headers).

Add uploading of log files and traceback-with-locals attachments to S3 for Sentry error reports. The log-collection logic now matches the minds log layout: a flat logs directory (`~/.minds/logs`) containing the live Python backend JSONL log, its timestamp-suffixed rotated siblings, and the Electron log, all gzip-compressed on upload. Sentry's `log_folder` is now the minds logs directory (exposed as `WorkspacePaths.log_dir`) rather than the data directory.

Select the Sentry DSN and S3 bucket from the activated minds env (`minds env activate`): production reports to the production Sentry project and bucket, staging to the staging project and bucket, and every other env (dev-*, ci-*, or no activated env) to the dev Sentry project with no S3 uploads (so dev machines never ship potentially-sensitive attachments off-box).

S3 attachment uploads remain opt-in via the `MINDS_SENTRY_S3_UPLOADS` env var (default off, even in production and staging), since the uploaded logs and traceback-with-locals attachments can carry potentially-sensitive data. When enabled, the bucket follows the environment; development never uploads regardless of the flag.

Report the desktop app version (from `package.json`) as the Sentry release and the git SHA the build was cut from as the `git_sha` tag. The Electron launcher passes both to the Python backend via `MINDS_RELEASE_ID`/`MINDS_GIT_SHA` (resolving the SHA live from the checkout in dev and from the build-time `build-info.json` in packaged builds); bare source runs fall back to reading `package.json` and report an `unknown` SHA.

Do not attach any user PII to Sentry error reports: the unused user-context wiring (`global_user_context` / `sentry_sdk.set_user`) has been removed, and `send_default_pii=False` is kept.

Flush Sentry (and any pending S3 attachment uploads) during the desktop client's shutdown teardown, so errors captured late in a session -- including any logged while shutting down -- are sent before the process exits.
