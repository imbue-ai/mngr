# Changelog - remote_service_connector

A concise, human-friendly summary of changes for the `remote_service_connector` app. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New no-auth `GET /health/liveness` route returning `{"status": "ok"}`.
- Added: New R2 bucket routes (`/buckets/*` and `/bucket-keys/*`), gated to paid accounts: create a bucket (with a default scoped key), list / inspect / destroy buckets, and mint / list / revoke additional bucket-scoped keys (read-only or read-write). Each key is an account-owned Cloudflare API token scoped to a single bucket; the S3 Access Key ID is the token id and the Secret Access Key is the SHA-256 of the token value (returned once, never stored). Only key metadata is persisted, in a new `r2_keys` table (migration `004_r2_keys.sql`). Buckets are listed straight from the R2 API with an in-code owner-prefix re-check. Destroying a bucket refuses if it is non-empty and otherwise cascades to revoke its keys.

### Changed

- Changed: `CLOUDFLARE_API_TOKEN` must now be an account-owned (`cfat_`) token carrying `Workers R2 Storage: Edit` + `Account API Tokens: Edit` (on top of the existing tunnel/DNS/Access/KV permissions), and R2 must be enabled on the Cloudflare account. See the README for the full migration.
- Changed: Renamed `vps_ip` → `vps_address` end-to-end across API models (`LeaseResult`, `LeasedHostInfo`, `LeaseHostResponse`), all Python call sites, and the `pool_hosts.vps_ip` DB column (migration `003_vps_address.sql`). The field can now hold a public IPv4 or a DNS hostname.
- Changed: `DELETE /tunnels/{name}` and `POST /hosts/{id}/release` are now idempotent at the HTTP layer — a second call returns 200 with `{"status": "already_deleted"}` / `{"status": "already_released"}` instead of 404.
- Changed: `_authenticate_supertokens` now passes `override_global_claim_validators=lambda *_: []` so the explicit `is_verified` check fires for unverified tokens instead of being shadowed by the SDK's generic `Invalid token` rejection. `_get_user_id_from_access_token` similarly skips claim validation so `/auth/session/revoke` works for unverified users.
- Changed: Connector's twelve `async def` endpoints (plus `_build_session_tokens`) have been converted to sync `def`, with SuperTokens recipe imports switched from `asyncio` modules to `syncio` equivalents. The OAuth callback endpoints bridge to async-only methods via `supertokens_python.async_to_sync_wrapper.sync`.
- Changed: Raised the `supertokens-python` floor from `>=0.27.0` to `>=0.31.3` so the repo-wide `uv lock --upgrade` doesn't backtrack the auth library to 0.30.3 (which would also cap `aiosmtplib<4`); leaves `aiosmtplib` at 5.x.

### Fixed

- Fixed: Connector auth endpoints no longer 500 on `/auth/session/revoke`, `/auth/email/is-verified`, and `/auth/email/send-verification` — these had been calling SuperTokens' `syncio.get_user` / `syncio.get_session_without_request_response` from inside an `async def`, where the syncio wrapper's `loop.run_until_complete` hit "RuntimeError: This event loop is already running" against the live FastAPI/uvicorn loop.

## [v0.2.7] - 2026-05-11

### Changed

- Changed: `remote_service_connector.add_service` is now idempotent; updating an access list no longer fails Cloudflare 81053 ("DNS record already exists").
- Changed: Connector schema migration replaces `pool_hosts.version` with `attributes JSONB`; legacy `version` callers are folded into `attributes` automatically.
