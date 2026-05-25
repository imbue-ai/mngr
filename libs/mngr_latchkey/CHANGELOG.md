# Changelog - mngr_latchkey

A concise, human-friendly summary of changes for the `mngr_latchkey` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr latchkey admin-jwt` and `mngr latchkey gateway-info` subcommands; `LatchkeyForwardInfo.gateway_port` stamped for non-spawning consumers.
- Added: Bundled `minds-api-proxy` Latchkey extension that reverse-proxies `/minds-api-proxy` to the minds desktop client's bare-origin "Minds API"; upstream URL read from `LATCHKEY_EXTENSION_MINDS_API_URL`.
- Added: Typed `permission-requests` schema with `{agent_id, rationale, type, payload}`; new `POST /permission-requests/approve/<id>` endpoint that merges `effect.rules` + `effect.schemas` into the target `permissions.json`. Pending requests live under `permission_requests/v2/`.
- Added: File-sharing permission effect targets the new WebDAV mount with required `access: READ | WRITE` field; per-file permission schemas embed access mode and absolute path in their name. `COPY` and `MOVE` are intentionally excluded from WRITE.
- Added: `GET /permissions/available` and `GET /permissions/available/<service_name>` catalog endpoints on the `permissions` extension; default permissions seeded for every new agent broadened to allow self-read of permissions plus reading the catalog entries.
- Added: `LatchkeyEncryptionKeyPermissionError` raised when the on-disk encryption-key file has group/other access bits set.

### Changed

- Changed: Bumped bundled Latchkey to 2.11.3.
- Changed: Switched mngr-latchkey + minds permission management to latchkey 2.9.0's `permission_requests` / `permissions` gateway extensions; `LATCHKEY_MIN_VERSION` bumped to 2.9.0.
- Changed: Regenerated CLI docs for `mngr latchkey`.
- Changed: `permission-requests` POST body now expects `{agent_id, scope, permissions, rationale}` instead of `service_name`; allowed WebDAV `method` enum expanded to the full read/write/query/lock/copy/delete set.
- Changed: `LatchkeyGatewayClient.get_available_services` now returns a typed `dict[str, AvailableServiceEntry]` (pydantic-validated); wire-shape failures surface as `LatchkeyGatewayClientError`.
- Changed: Encryption key is no longer cached on the long-lived `Latchkey` pydantic model — `Latchkey._load_encryption_key()` reads it on every spawn call so the secret only lives in parent-process memory for one call frame.
- Changed: Bumped pinned `imbue-mngr` / `imbue-common` / `concurrency-group` versions.

### Fixed

- Fixed: `POST /permission-requests/approve/<id>` preserves symlinks at the approval target — the atomic-write helper `lstat`s the target and resolves symlinks via `realpath` before computing the temp path, so per-agent opaque symlinks swung by `mngr latchkey link-permissions` stay in place.
- Fixed: Race condition in per-directory encryption-key resolution where a concurrent reader observed an empty key file mid-write; the key file is now published atomically via temp + `fsync` + `os.link`.

## [v0.2.8] - 2026-05-13

### Added

- Added: New `imbue-mngr-latchkey` package owning the shared latchkey gateway lifecycle, per-agent wiring, and reverse SSH tunnel; ships as a `mngr` plugin with `mngr latchkey forward` / `create-agent-env` / `link-permissions` subcommands plus a `LatchkeyForwardSupervisor`.

### Changed

- Changed: Latchkey state is now keyed per-host instead of per-agent — `finalize_agent_permissions` → `finalize_host_permissions`, `permissions_path_for_agent` → `permissions_path_for_host`, and `mngr latchkey link-permissions` takes `--host-id`.

### Removed

- Removed: Latchkey on-disk gateway record (`<plugin_data_dir>/latchkey_gateway.json`) and all cross-process gateway adoption helpers — `LatchkeyForwardSupervisor` guarantees one `mngr latchkey forward` per directory.

### Fixed

- Fixed: `mngr latchkey forward` no longer dies with its parent — the parent-death watcher was removed and SIGHUP is wired into the clean coupled-lifetime shutdown path for the interactive case.
