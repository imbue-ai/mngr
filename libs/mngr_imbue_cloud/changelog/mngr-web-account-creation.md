Rework auth around the browser login flow.

- New `mngr imbue_cloud auth login`: opens the connector's hosted accounts page in the system browser and exchanges the loopback-delivered one-time code (PKCE S256) for this machine's own session. `--url-file` exposes the sign-in URL to embedders (the minds copy-the-link fallback); `--no-browser` prints it.

- `auth oauth` is removed (subsumed by `auth login`, whose hosted page offers Continue-with-Google). `auth signin` / `auth signup` remain the documented headless path.

- Email verification is non-blocking: the pending-session machinery is gone, every successful auth response counts as signed in immediately, and `auth is-verified` is a plain status query. Session files written by older versions (with the pending flag) still parse.

- `auth signout` now revokes only this machine's session; pass `--all-devices` to revoke every session for the account (falls back to revoke-all against connectors without the device-scoped endpoint). A failed server-side revocation is no longer silent: the emitted JSON carries `server_session_revoked` (with a stderr warning) for the default sign-out, and `--all-devices` fails outright, keeping the local session for a retry.

- `auth login` probes the connector for the hosted accounts pages before opening anything and fails fast with an actionable "connector too old -- run `minds env deploy`" error against a stale dev/CI env (the device-token exchange maps a 404 to the same error as a safety net).

- The connector's structured `email_not_verified` 403 now surfaces as a typed error (`ImbueCloudEmailNotVerifiedError`, JSON `code: email_not_verified` plus the email) so embedders can prompt verification contextually.
