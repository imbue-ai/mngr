# Changelog - mngr_latchkey

A concise, human-friendly summary of changes for the `mngr_latchkey` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr latchkey admin-jwt` and `mngr latchkey gateway-info` subcommands; `LatchkeyForwardInfo.gateway_port` stamped for non-spawning consumers.

### Changed

- Changed: Bumped bundled Latchkey to 2.11.1.
- Changed: Switched mngr-latchkey + minds permission management to latchkey 2.9.0's `permission_requests` / `permissions` gateway extensions; `LATCHKEY_MIN_VERSION` bumped to 2.9.0.
- Changed: Regenerated CLI docs for `mngr latchkey`.
- Changed: `permission-requests` gateway extension now expects POST bodies with `agent_id` / `scope` / `permissions` / `rationale` in place of the previous `service_name` field; pending requests stored under `<latchkey-directory>/permission_requests/v1/`.
- Changed: `permissions` gateway extension exposes new catalog endpoints `GET /permissions/available` and `GET /permissions/available/<service_name>`, backed by a `services.json` data file materialized alongside the extensions.
- Changed: Default per-agent permissions broadened to allow `GET /permissions/self` and `GET /permissions/available/<service_name>` (under a path-pattern Detent schema) in addition to `POST /permission-requests`.
- Changed: `LatchkeyGatewayClient.get_available_services` now returns a typed `dict[str, AvailableServiceEntry]` (pydantic-validated); wire-shape failures surface as `LatchkeyGatewayClientError`.
- Changed: Stopped caching the per-directory encryption key on the long-lived `Latchkey` model — `_load_encryption_key()` reads (or mints on first call) the key on every subprocess-spawn call, narrowing the in-memory lifetime to a single env-builder call frame.
- Changed: Project now participates in the per-project changelog layout (per-project `changelog/`, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md`).

### Fixed

- Fixed: Race condition in per-directory encryption-key resolution where a concurrent caller could read the on-disk key file mid-write; key file is now published atomically via temp-file + fsync + `os.link`.

### Security

- Security: `load_or_create_encryption_key` now validates the on-disk key file's permission bits every load; any group/other access bit raises `LatchkeyEncryptionKeyPermissionError` with a `chmod 600` hint. `LATCHKEY_ENCRYPTION_KEY` env override still wins.

## [v0.2.8] - 2026-05-13

### Added

- Added: New `imbue-mngr-latchkey` package owning the shared latchkey gateway lifecycle, per-agent wiring, and reverse SSH tunnel; ships as a `mngr` plugin with `mngr latchkey forward` / `create-agent-env` / `link-permissions` subcommands plus a `LatchkeyForwardSupervisor`.

### Changed

- Changed: Latchkey state is now keyed per-host instead of per-agent — `finalize_agent_permissions` → `finalize_host_permissions`, `permissions_path_for_agent` → `permissions_path_for_host`, and `mngr latchkey link-permissions` takes `--host-id`.

### Removed

- Removed: Latchkey on-disk gateway record (`<plugin_data_dir>/latchkey_gateway.json`) and all cross-process gateway adoption helpers — `LatchkeyForwardSupervisor` guarantees one `mngr latchkey forward` per directory.

### Fixed

- Fixed: `mngr latchkey forward` no longer dies with its parent — the parent-death watcher was removed and SIGHUP is wired into the clean coupled-lifetime shutdown path for the interactive case.
