Minds-managed agents can now spawn peer minds and follow their creation through to completion: the desktop client's `/api/create-agent`, `/api/create-agent/{id}/status`, and `/api/create-agent/{id}/logs` endpoints accept the per-startup central `MINDS_API_KEY` as a bearer token (injected agent-invisibly by the latchkey gateway's `minds-api-proxy` extension) as an alternative to the browser session cookie.

The first peer-management attempt goes through the standard permission-request dialog ("Peer minds"); the user can grant `any` for one-click full access or any subset of `minds-create`, `minds-status`, and `minds-logs`, and subsequent calls run silently.

`minds run` now backfills the new `minds` scope and named-permission schemas into pre-existing per-host `latchkey_permissions.json` files at startup, so agents created before this feature can be granted the new scope.

The permission dialog now treats a latchkey-reported `unknown` credential status as "proceed" (only `missing` / `invalid` trigger credential setup), so scopes without a registered latchkey service -- like `minds` -- no longer show a spurious "Manual credential setup required" panel.
