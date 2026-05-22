# Changelog - remote_service_connector

A concise, human-friendly summary of changes for the `remote_service_connector` app. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New no-auth `GET /health/liveness` route returning `{"status": "ok"}` for liveness probes.

### Changed

- Changed: `DELETE /tunnels/{name}` and `POST /hosts/{id}/release` are now idempotent at the HTTP layer — a second call against an already-deleted tunnel or already-released host returns 200 with a `"status": "already_*"` payload instead of 404.
- Changed: Renamed `vps_ip` → `vps_address` end-to-end (API models, Python call sites, and the `pool_hosts.vps_ip` DB column); migration ships as `003_vps_address.sql`. The field can hold a public IPv4 or a DNS hostname.
- Changed: Connector's `async def` endpoints converted to sync `def` (twelve endpoints plus `_build_session_tokens`); SuperTokens recipe imports switched from their `asyncio` modules to `syncio`, and the three remaining async-only OAuth provider calls go through `supertokens_python.async_to_sync_wrapper.sync`. Each newly-sync endpoint is wrapped in `with handle_endpoint_errors():`.
- Changed: Adopted per-project changelog layout (`changelog/` dir, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md` at the project root).

### Fixed

- Fixed: `_authenticate_supertokens` now passes `override_global_claim_validators=lambda *_: []` so the explicit `if not is_verified: raise 401 "Email not verified"` check fires for unverified tokens instead of being shadowed by the SDK's generic "Invalid token" rejection; `/auth/session/revoke`, `/auth/email/is-verified`, and `/auth/email/send-verification` no longer 500 for sign-out flows.

## [v0.2.7] - 2026-05-11

### Changed

- Changed: `remote_service_connector.add_service` is now idempotent; updating an access list no longer fails Cloudflare 81053 ("DNS record already exists").
- Changed: Connector schema migration replaces `pool_hosts.version` with `attributes JSONB`; legacy `version` callers are folded into `attributes` automatically.
