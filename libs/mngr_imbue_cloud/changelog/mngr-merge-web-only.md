Merge the web-only work (`mngr/hopefully-last-web-details`) into main. Reworks auth and sharing around the browser login flow:

New `mngr imbue_cloud auth login`: opens the connector's hosted accounts page in the system browser and exchanges the loopback-delivered one-time code (PKCE S256) for this machine's own session (`--url-file`/`--no-browser` expose the sign-in URL). `auth oauth` is removed (subsumed by `auth login`); `auth signin`/`auth signup` remain the headless path.

Email verification is non-blocking: the pending-session machinery is gone, every successful auth response counts as signed in immediately, and `auth is-verified` is a plain status query. `auth signout` now revokes only this machine's session (`--all-devices` revokes all). The connector's `email_not_verified` 403 surfaces as a typed `ImbueCloudEmailNotVerifiedError`.

New `mngr imbue_cloud hosts enable-sharing <host_ref>` for server-side share bring-up on a leased host (idempotent; re-running rotates the relay token), and `shares create --entry-label` records the workspace's shell-service origin label so the hosted web chrome knows the routable origin to enter and health-probe.
