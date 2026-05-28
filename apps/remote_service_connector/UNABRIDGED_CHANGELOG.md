# Unabridged Changelog - remote_service_connector

Full, unedited changelog entries consolidated nightly from individual files in `apps/remote_service_connector/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

- `_authenticate_supertokens` now passes
  ``override_global_claim_validators=lambda *_: []`` to the SuperTokens
  session getter so the explicit ``if not is_verified: raise 401
  "Email not verified"`` check fires for unverified tokens instead of
  being shadowed by the SDK's generic ``Invalid token`` rejection. The
  matching ``_get_user_id_from_access_token`` helper also skips claim
  validation so flows like ``/auth/session/revoke`` (sign-out) work
  for unverified users -- they legitimately need to sign out of a
  session they never finished verifying. (F6)
- Connector exposes a new no-auth ``GET /health/liveness`` route
  returning ``{"status": "ok"}``. (F2)
- ``DELETE /tunnels/{name}`` and ``POST /hosts/{id}/release`` are now
  idempotent at the HTTP layer: a second call against an already-
  deleted tunnel or already-released host returns 200 with
  ``{"status": "already_deleted"}`` / ``{"status": "already_released"}``
  instead of 404. Clients retrying after a transient error no longer
  have to special-case 404. (F7, F30)
- Renamed `vps_ip` -> `vps_address` end-to-end: API models
  (`LeaseResult`, `LeasedHostInfo`, `LeaseHostResponse`), all Python
  call sites, AND the `pool_hosts.vps_ip` DB column. Migration ships
  as `apps/remote_service_connector/migrations/003_vps_address.sql`
  (idempotent rename). The field can hold a public IPv4 or a DNS
  hostname (e.g. OVH's `vps-eec8860b.vps.ovh.us`).
- Connector auth endpoints no longer 500 on `/auth/session/revoke`,
  `/auth/email/is-verified`, `/auth/email/send-verification`. The connector's
  twelve `async def` endpoints (plus the `_build_session_tokens` helper)
  have been converted to sync `def`, with the SuperTokens recipe imports
  switched from their `asyncio` modules to the `syncio` equivalents. The
  three broken endpoints were calling SuperTokens' `syncio.get_user` /
  `syncio.get_session_without_request_response` from inside an
  `async def`, where the syncio wrapper's `loop.run_until_complete` hit
  "RuntimeError: This event loop is already running" against the live
  FastAPI/uvicorn loop and produced bare 500s. The conversion makes the
  bug class structurally impossible (no event loop is running in
  FastAPI's threadpool workers) and also aligns the file with the
  monorepo style guide's prohibition on `async`/`asyncio`. Each
  newly-sync endpoint is wrapped in `with handle_endpoint_errors():` so
  error handling stays uniform across the file. The two OAuth callback
  endpoints still need to bridge to async-only methods on SuperTokens'
  `Provider` object (`get_authorisation_redirect_url`,
  `exchange_auth_code_for_oauth_tokens`, `get_user_info`); those three
  calls go through `supertokens_python.async_to_sync_wrapper.sync`, the
  same wrapper SuperTokens' own syncio modules use internally -- safe
  here because FastAPI runs sync def endpoints in a threadpool worker
  with no live event loop.

## 2026-05-06

- remote_service_connector: `add_service` is now idempotent. Updating the access list on a previously-shared service (which re-runs the full create-tunnel/add-service/set-auth chain) no longer fails with Cloudflare error 81053 ("DNS record already exists"). When a CNAME or ingress rule for the hostname is already in place pointing at the same tunnel, it is reused; if the per-service auth policy was already customized via `set_service_auth`, the tunnel default is no longer reapplied on top.

- Connector schema migration: `pool_hosts.version` is replaced with a flexible `attributes JSONB` column. `/hosts/lease` matches with `attributes @> request_attributes`. Backwards-compatible: legacy callers can still pass `version` as a top-level field; the connector folds it into attributes automatically.
