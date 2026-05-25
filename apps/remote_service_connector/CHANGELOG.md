# Changelog - remote_service_connector

A concise, human-friendly summary of changes for the `remote_service_connector` app. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New no-auth `GET /health/liveness` route returning `{"status": "ok"}`.

### Changed

- Changed: `DELETE /tunnels/{name}` and `POST /hosts/{id}/release` are now idempotent at the HTTP layer — second call returns 200 with `{"status": "already_deleted"}` / `{"status": "already_released"}` instead of 404.
- Changed: Renamed `vps_ip` → `vps_address` end-to-end across API models (`LeaseResult`, `LeasedHostInfo`, `LeaseHostResponse`), call sites, and the `pool_hosts.vps_ip` DB column (migration `003_vps_address.sql`). The field can hold a public IPv4 or a DNS hostname.
- Changed: Connector's twelve `async def` endpoints (plus `_build_session_tokens`) converted to sync `def`; SuperTokens recipe imports switched from `asyncio` to `syncio`. Auth endpoints no longer 500 on `/auth/session/revoke`, `/auth/email/is-verified`, `/auth/email/send-verification`.

### Fixed

- Fixed: `_authenticate_supertokens` passes `override_global_claim_validators=lambda *_: []` so the explicit "Email not verified" check fires for unverified tokens instead of being shadowed by the SDK's generic "Invalid token" rejection; `_get_user_id_from_access_token` skips claim validation so `/auth/session/revoke` works for unverified users signing out.

## [v0.2.7] - 2026-05-11

### Changed

- Changed: `remote_service_connector.add_service` is now idempotent; updating an access list no longer fails Cloudflare 81053 ("DNS record already exists").
- Changed: Connector schema migration replaces `pool_hosts.version` with `attributes JSONB`; legacy `version` callers are folded into `attributes` automatically.
