# Changelog - mngr_latchkey

A concise, human-friendly summary of changes for the `mngr_latchkey` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr latchkey admin-jwt` and `mngr latchkey gateway-info` subcommands; `LatchkeyForwardInfo.gateway_port` stamped for non-spawning consumers.
- Added: New bundled `minds-api-proxy` latchkey gateway extension that reverse-proxies requests under `/minds-api-proxy` to the minds desktop client's bare-origin Minds API, with the upstream URL read at request time from `LATCHKEY_EXTENSION_MINDS_API_URL`; `LatchkeyForwardSupervisor.extra_env` publishes the env var to the detached supervisor on every `minds run` startup.
- Added: `POST /permission-requests/approve/<request_id>` endpoint that merges the pending request's `effect.rules` + `effect.schemas` into the stored `target` permissions.json.
- Added: New `GET /permissions/available` / `GET /permissions/available/<service_name>` catalog endpoints, backed by a `services.json` data file materialized alongside the `.mjs` extensions at gateway-spawn time.
- Added: `register_agent_for_host(plugin_data_dir, host_id, agent_id)` (in `agent_setup.py`) authorizes an agent to reach the Minds API by appending its id to the host permissions file's allowed-agent list — an idempotent, atomic edit that seeds from the baseline when no file exists yet.
- Added: `mngr latchkey register-agent --host-id ID --agent-id ID` CLI wrapping that helper for operators (documented in the README).
- Added: `imbue.mngr_latchkey.store.load_permissions(path)` public reader, symmetric with `save_permissions`.

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
- Changed: `minds-api-proxy` gateway extension now authenticates forwarded requests to the upstream Minds API on the agent's behalf — when `LATCHKEY_EXTENSION_MINDS_API_KEY` is set it overwrites the inbound `Authorization` header with `Bearer <key>`, so agents never see the key and cannot spoof one. With the env var unset, the header is forwarded unchanged (used by tests).
- Changed: The agent baseline permissions file now enforces per-agent Minds API isolation via two cooperating rules — a deny rule for any `/minds-api-proxy/api/v1/agents/<id>/...` whose `<id>` is absent from an allowed-agent `anyOf` list, then a generic allow rule for listed agents — so an agent on one host cannot reach the Minds API on behalf of an agent on another.
- Changed: `mngr_latchkey/ssh_tunnel.py` is removed; `SSHTunnelManager`, `RemoteSSHInfo`, and `SSHTunnelError` are now imported from `imbue.mngr_forward.ssh_tunnel`, the single monorepo SSH-tunneling implementation (which absorbed latchkey's exponential-backoff repair loop, `agent_id` tagging, and `remove_reverse_tunnels_for_agent`).
- Changed: `Latchkey.auth_browser` now transparently recovers from latchkey's "Service `<name>` requires preparation first" error by running `latchkey auth browser-prepare <service>` and retrying `latchkey auth browser <service>` once, so callers (e.g. minds' predefined-permission grant flow) succeed on the first user-visible attempt instead of failing with a confusing error.

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
