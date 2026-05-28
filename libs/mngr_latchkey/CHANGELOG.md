# Changelog - mngr_latchkey

A concise, human-friendly summary of changes for the `mngr_latchkey` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr latchkey admin-jwt` and `mngr latchkey gateway-info` subcommands; `LatchkeyForwardInfo.gateway_port` stamped for non-spawning consumers.
- Added: New bundled `minds-api-proxy` latchkey gateway extension that reverse-proxies requests under `/minds-api-proxy` to the minds desktop client's bare-origin Minds API, with the upstream URL read at request time from `LATCHKEY_EXTENSION_MINDS_API_URL`; `LatchkeyForwardSupervisor.extra_env` publishes the env var to the detached supervisor on every `minds run` startup.
- Added: `POST /permission-requests/approve/<request_id>` endpoint that merges the pending request's `effect.rules` + `effect.schemas` into the stored `target` permissions.json.
- Added: New `GET /permissions/available` / `GET /permissions/available/<service_name>` catalog endpoints, backed by a `services.json` data file materialized alongside the `.mjs` extensions at gateway-spawn time.
- Added: `mngr latchkey register-agent --host-id ID --agent-id ID` CLI and a matching `imbue.mngr_latchkey.agent_setup.register_agent_for_host(plugin_data_dir, host_id, agent_id)` library helper that idempotently appends an agent id to the per-host permissions file's allowed-agent `anyOf` list.
- Added: `imbue.mngr_latchkey.store.load_permissions` — new public reader symmetric with `save_permissions`, used by `register_agent_for_host`.
- Added: `permissions` extension grew CRUD for inline detent schemas — `POST /permissions/schemas?path=<file>&schema_name=<name>` adds or replaces an inline schema (body is the JSON schema definition), `DELETE /permissions/schemas?...` removes it. Schema names must match `^[A-Za-z0-9][A-Za-z0-9._-]*$`.

### Changed

- Changed: Bumped bundled Latchkey to 2.11.1.
- Changed: Switched mngr-latchkey + minds permission management to latchkey 2.9.0's `permission_requests` / `permissions` gateway extensions; `LATCHKEY_MIN_VERSION` bumped to 2.9.0.
- Changed: `permission-requests` extension now uses a typed request schema — `POST /permission-requests` takes `{agent_id, rationale, type, payload}`, where `type` is `"predefined"` or `"file-sharing"`. Pending requests are persisted with new `target` + `effect` fields under `permission_requests/v2/`.
- Changed: File-sharing permission effect now targets the new WebDAV mount — the per-file permission attaches to the pre-existing `latchkey-self` scope, schema pins `path` to the WebDAV URL `/minds-api-proxy/api/v1/files<absolute_path>` (regex `pattern` so trailing slashes and nested sub-paths are covered transitively), and `method` enum expanded to the full set of WebDAV verbs.
- Changed: File-sharing requests now carry a required `access` field (`READ` / `WRITE`); `READ` unlocks `GET` / `HEAD` / `OPTIONS` / `PROPFIND`, `WRITE` additionally unlocks `PUT` / `DELETE` / `PROPPATCH` / `MKCOL` / `LOCK` / `UNLOCK`. `COPY` and `MOVE` are intentionally excluded.
- Changed: Default permissions seeded for every new agent are broadened to let the agent read its own current permissions (`GET /permissions/self`) and the per-service catalog entry (`GET /permissions/available/<service_name>`).
- Changed: `LatchkeyGatewayClient.get_available_services` now returns a typed `dict[str, AvailableServiceEntry]` (pydantic-validated) instead of an untyped `dict[str, object]`.
- Changed: Stop caching the latchkey per-directory encryption key on the long-lived `Latchkey` pydantic model; `Latchkey._load_encryption_key()` reads (and on first call mints) the key on every subprocess-spawn call so the secret only lives in parent memory for the duration of one env-builder call frame.
- Changed: `load_or_create_encryption_key` now validates the on-disk key file's permission bits every load; any group/other access raises `LatchkeyEncryptionKeyPermissionError` with a `chmod 600 <path>` hint.
- Changed: The `minds-api-proxy` gateway extension now authenticates the forwarded request to the upstream Minds API on the agent's behalf — it reads `LATCHKEY_EXTENSION_MINDS_API_KEY` on every request and overwrites the inbound `Authorization` header with `Bearer <key>`, so agents never see the value and cannot spoof one.
- Changed: Per-agent Minds API proxy permissions are now driven by a plain JSON `anyOf` list of allowed agent ids in the baseline permissions file (with two cooperating rules — a deny rule for unknown ids and a generic allow rule for the proxy subtree), instead of installing a per-agent scope + permission + rule on creation. The shared `minds-api-proxy-notifications` baseline grant is gone; notifications are gated through the same allowed-agent list.

### Fixed

- Fixed: Race condition in per-directory encryption-key resolution where a concurrent caller could read the on-disk key file mid-write; the file is now published atomically via temp file + `fsync` + `os.link` so the final path only ever exists with complete contents.
- Fixed: `POST /permission-requests/approve/<id>` no longer replaces a symlinked `permissions.json` target with a regular file. The atomic-write helper now `lstat`s the target and resolves symlinks via `realpath` before the temp-file rename, so per-agent symlinks (e.g. those swung in by `mngr latchkey link-permissions`) stay intact and shared canonical permissions remain in sync.

## [v0.2.8] - 2026-05-13

### Added

- Added: New `imbue-mngr-latchkey` package owning the shared latchkey gateway lifecycle, per-agent wiring, and reverse SSH tunnel; ships as a `mngr` plugin with `mngr latchkey forward` / `create-agent-env` / `link-permissions` subcommands plus a `LatchkeyForwardSupervisor`.

### Changed

- Changed: Latchkey state is now keyed per-host instead of per-agent — `finalize_agent_permissions` → `finalize_host_permissions`, `permissions_path_for_agent` → `permissions_path_for_host`, and `mngr latchkey link-permissions` takes `--host-id`.

### Removed

- Removed: Latchkey on-disk gateway record (`<plugin_data_dir>/latchkey_gateway.json`) and all cross-process gateway adoption helpers — `LatchkeyForwardSupervisor` guarantees one `mngr latchkey forward` per directory.

### Fixed

- Fixed: `mngr latchkey forward` no longer dies with its parent — the parent-death watcher was removed and SIGHUP is wired into the clean coupled-lifetime shutdown path for the interactive case.
