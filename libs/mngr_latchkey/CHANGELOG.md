# Changelog - mngr_latchkey

A concise, human-friendly summary of changes for the `mngr_latchkey` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr latchkey admin-jwt` and `mngr latchkey gateway-info` subcommands; `LatchkeyForwardInfo.gateway_port` stamped for non-spawning consumers.
- Added: `GET /permissions/available` and `GET /permissions/available/<service_name>` catalog endpoints on the `permissions` gateway extension, backed by a `services.json` data file materialized into `LATCHKEY_DIRECTORY/extensions/` alongside the `.mjs` files.
- Added: `LatchkeyEncryptionKeyPermissionError` raised on every load when the on-disk key file isn't owner-only, with a copy-pasteable `chmod 600` hint.

### Changed

- Changed: Bumped bundled Latchkey to 2.11.1.
- Changed: Switched mngr-latchkey + minds permission management to latchkey 2.9.0's `permission_requests` / `permissions` gateway extensions; `LATCHKEY_MIN_VERSION` bumped to 2.9.0.
- Changed: Regenerated CLI docs for `mngr latchkey`.
- Changed: `permission-requests` POST bodies now use `agent_id` / `scope` / `permissions` / `rationale` (replacing `service_name`); pending requests live under `<latchkey-directory>/permission_requests/v1/`.
- Changed: Default agent permissions broadened to allow reading the agent's own permissions (`GET /permissions/self`) and the per-service catalog entry (`GET /permissions/available/<service_name>`).
- Changed: `LatchkeyGatewayClient.get_available_services` now returns a typed `dict[str, AvailableServiceEntry]` (pydantic-validated); wire-shape validation surfaces as `LatchkeyGatewayClientError`.
- Changed: Per-directory encryption key is no longer cached on the long-lived `Latchkey` model; `_load_encryption_key()` reads (and on first call mints) the key per subprocess-spawn call, so the secret only lives in parent-process memory for the duration of one call frame.
- Changed: Bumped pinned `imbue-mngr` / `imbue-common` / `concurrency-group` versions to match the current monorepo.
- Changed: Adopted per-project changelog layout (`changelog/` dir, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md` at the project root).

### Fixed

- Fixed: Race condition in per-directory encryption-key resolution where a concurrent caller could read the on-disk key file mid-write — the key file is now published atomically via a sibling temp file + `fsync` + `os.link`.

## [v0.2.8] - 2026-05-13

### Added

- Added: New `imbue-mngr-latchkey` package owning the shared latchkey gateway lifecycle, per-agent wiring, and reverse SSH tunnel; ships as a `mngr` plugin with `mngr latchkey forward` / `create-agent-env` / `link-permissions` subcommands plus a `LatchkeyForwardSupervisor`.

### Changed

- Changed: Latchkey state is now keyed per-host instead of per-agent — `finalize_agent_permissions` → `finalize_host_permissions`, `permissions_path_for_agent` → `permissions_path_for_host`, and `mngr latchkey link-permissions` takes `--host-id`.

### Removed

- Removed: Latchkey on-disk gateway record (`<plugin_data_dir>/latchkey_gateway.json`) and all cross-process gateway adoption helpers — `LatchkeyForwardSupervisor` guarantees one `mngr latchkey forward` per directory.

### Fixed

- Fixed: `mngr latchkey forward` no longer dies with its parent — the parent-death watcher was removed and SIGHUP is wired into the clean coupled-lifetime shutdown path for the interactive case.
