# Changelog - remote_service_connector

A concise, human-friendly summary of changes for the `remote_service_connector` app. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: No-auth `GET /health/liveness` route returning `{"status": "ok"}`.

### Changed

- Changed: Renamed `vps_ip` → `vps_address` end-to-end across API models (`LeaseResult`, `LeasedHostInfo`, `LeaseHostResponse`), call sites, and the `pool_hosts.vps_ip` DB column (migration `003_vps_address.sql` ships an idempotent rename); field can hold a public IPv4 or a DNS hostname.
- Changed: `DELETE /tunnels/{name}` and `POST /hosts/{id}/release` are now idempotent at the HTTP layer — a second call against an already-deleted tunnel / already-released host returns 200 with `{"status": "already_deleted"}` / `{"status": "already_released"}` instead of 404.
- Changed: All twelve `async def` endpoints (plus `_build_session_tokens`) converted to sync `def` (SuperTokens recipes switched to `syncio` modules); OAuth callbacks bridge to async-only methods via `supertokens_python.async_to_sync_wrapper.sync`.
- Changed: Project now participates in the per-project changelog layout (per-project `changelog/`, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md`).

### Fixed

- Fixed: `_authenticate_supertokens` now passes `override_global_claim_validators=lambda *_: []` so the explicit "Email not verified" 401 fires for unverified tokens instead of being shadowed by the SDK's generic "Invalid token" rejection; sign-out / verification flows for unverified users now work.
- Fixed: `/auth/session/revoke`, `/auth/email/is-verified`, `/auth/email/send-verification` no longer 500 — the sync conversion eliminated the `RuntimeError: This event loop is already running` from calling `syncio.get_user` / `get_session_without_request_response` from inside `async def` against FastAPI's live loop.

## [v0.2.7] - 2026-05-11

### Changed

- Changed: `remote_service_connector.add_service` is now idempotent; updating an access list no longer fails Cloudflare 81053 ("DNS record already exists").
- Changed: Connector schema migration replaces `pool_hosts.version` with `attributes JSONB`; legacy `version` callers are folded into `attributes` automatically.
