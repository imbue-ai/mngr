# Changelog - mngr_latchkey

A concise, human-friendly summary of changes for the `mngr_latchkey` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr latchkey admin-jwt` and `mngr latchkey gateway-info` subcommands; `LatchkeyForwardInfo.gateway_port` stamped for non-spawning consumers.
- Added: New typed permission-request schema (`{agent_id, rationale, type, payload}`) with `predefined` and `file-sharing` variants; pending requests stored under `permission_requests/v2/`; new `POST /permission-requests/approve/<id>` merges precomputed `effect.rules`/`effect.schemas` into the target permissions file.
- Added: Bundled `minds-api-proxy` Latchkey extension that reverse-proxies `/minds-api-proxy` to a minds-supplied upstream URL (`LATCHKEY_EXTENSION_MINDS_API_URL`); returns 503 when the env var is unset.
- Added: WebDAV-aware `file-sharing` permission effect with required `access: READ | WRITE` field; READ unlocks non-mutating verbs (`GET`/`HEAD`/`OPTIONS`/`PROPFIND`), WRITE adds the single-path mutating verbs (`PUT`/`DELETE`/`PROPPATCH`/`MKCOL`/`LOCK`/`UNLOCK`). `COPY`/`MOVE` are intentionally excluded. Per-file permission schema attaches to the pre-existing `latchkey-self` scope.
- Added: `GET /permissions/available` and `GET /permissions/available/<service_name>` catalog endpoints backed by a `services.json` data file materialized into `LATCHKEY_DIRECTORY/extensions/` at spawn time; agent baseline broadened to allow reading own permissions and per-service catalog entries.
- Added: `LatchkeyForwardSupervisor.extra_env` for publishing per-startup env vars (e.g. minds-api upstream URL) to the detached supervisor.

### Changed

- Changed: Bumped bundled Latchkey to 2.11.1.
- Changed: Switched mngr-latchkey + minds permission management to latchkey 2.9.0's `permission_requests` / `permissions` gateway extensions; `LATCHKEY_MIN_VERSION` bumped to 2.9.0.
- Changed: Regenerated CLI docs for `mngr latchkey`.
- Changed: `LatchkeyGatewayClient.get_available_services` now returns a typed `dict[str, AvailableServiceEntry]` (pydantic-validated) instead of an untyped dict; wire-shape validation surfaces as `LatchkeyGatewayClientError`.
- Changed: Per-directory encryption key is no longer cached on the `Latchkey` model — `_load_encryption_key()` reads/mints on each subprocess-spawn so the secret only lives in memory for one env-builder call frame.
- Changed: `load_or_create_encryption_key` validates the on-disk key file's permission bits every load; group/other access bits raise the new `LatchkeyEncryptionKeyPermissionError` with a copy-pasteable `chmod 600 <path>` hint.

### Fixed

- Fixed: Race condition in per-directory encryption-key resolution where a concurrent reader could observe an empty key string mid-write — the key file is now published atomically via write + `fsync` + `os.link`.

## [v0.2.8] - 2026-05-13

### Added

- Added: New `imbue-mngr-latchkey` package owning the shared latchkey gateway lifecycle, per-agent wiring, and reverse SSH tunnel; ships as a `mngr` plugin with `mngr latchkey forward` / `create-agent-env` / `link-permissions` subcommands plus a `LatchkeyForwardSupervisor`.

### Changed

- Changed: Latchkey state is now keyed per-host instead of per-agent — `finalize_agent_permissions` → `finalize_host_permissions`, `permissions_path_for_agent` → `permissions_path_for_host`, and `mngr latchkey link-permissions` takes `--host-id`.

### Removed

- Removed: Latchkey on-disk gateway record (`<plugin_data_dir>/latchkey_gateway.json`) and all cross-process gateway adoption helpers — `LatchkeyForwardSupervisor` guarantees one `mngr latchkey forward` per directory.

### Fixed

- Fixed: `mngr latchkey forward` no longer dies with its parent — the parent-death watcher was removed and SIGHUP is wired into the clean coupled-lifetime shutdown path for the interactive case.
