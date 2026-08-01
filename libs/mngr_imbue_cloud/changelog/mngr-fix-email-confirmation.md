Unverified email/password signups no longer count as signed in. A signup (or signin) whose email is not yet verified is saved as a *pending* session: its tokens are kept (they drive the verification poll) but the account is excluded from `mngr imbue_cloud auth list` and can never become the active account. A session store containing only pending sessions also no longer enables the implicit default `[providers.imbue_cloud]` instance.

- New `mngr imbue_cloud auth is-verified --account <email>` command: checks verification status against the connector and, once verified, promotes the pending session (it becomes the active, listed account -- the same activation a verified signin performs). `auth status` now reports `pending_verification`.

- `auth resend-verification` and the verification-status check authenticate with the caller's own access token, matching the connector's tightened endpoints, and `resend-verification` reports `sent: false` when the server-side cooldown suppressed the send.

- The connector client grew an injectable httpx transport (`transport` field) as a mock-free test seam, and the two verification calls now ride the shared transient-transport retry path.
