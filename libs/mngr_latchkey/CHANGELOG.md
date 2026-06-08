# Changelog - mngr_latchkey

A concise, human-friendly summary of changes for the `mngr_latchkey` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `imbue.mngr_latchkey.remote_gateway` for running the latchkey gateway on the VPS (the agent's outer host). Pins `LATCHKEY_VERSION = 2.15.1` and exposes a small public surface: `sync_credentials(host, latchkey_directory)` copies the local encrypted credential store to `~/.latchkey/` on the VPS; `sync_permissions(host, latchkey_directory, host_id)` copies the per-host permissions file (falling back to the deny-all default when no local file exists); and `provision_remote_gateway(host, host_id, container_ssh_user, container_ssh_port)` orchestrates installing the upstream `latchkey` CLI on the VPS, starting `latchkey gateway` bound to VPS loopback (with `LATCHKEY_ENCRYPTION_KEY` interpolated from the local encryption key), locating the agent's container by its `com.imbue.mngr.host-id` label, minting an ed25519 keypair authorized in the container via `docker exec`, and opening a reverse SSH tunnel from the VPS into the container so the agent reaches the VPS gateway via its unchanged `LATCHKEY_GATEWAY=http://127.0.0.1:INNER_PORT`. No-op when the outer host is the local machine; the detached gateway and tunnel are each idempotent via PID-file plus `/proc/<pid>/cmdline` marker (not `pgrep -f`, which would self-match the launching shell). Both syncs write atomically.
- Added: `LatchkeyDiscoveryHandler.start_remote_state_sync(concurrency_group)` keeps every known remote (VPS) host in sync with the desktop's latchkey state — initial inline sync per host, then a `watchdog` observer that pushes credential changes to every known remote host and per-host permission changes to that host. The observer's health is supervised on a checked concurrency-group strand so a silent failure surfaces loudly. Wired into `mngr latchkey forward`. Adds a `watchdog>=4.0` dependency.
- Added: `prepare_agent_latchkey(..., is_tunneled=True)` now also injects `LATCHKEY_GATEWAY_SECONDARY` into the agent's host env — the agent's URL for the per-VPS gateway as seen from inside the workspace container (`http://127.0.0.1:<INNER_PORT>`). Set for all tunneled agents (the endpoint is only live on genuinely-remote VPS-backed hosts; the URL is the agent's view either way) and omitted for on-host/DEV agents. Flows automatically to both `mngr latchkey create-agent-env` and the minds desktop client.
- Added: `INNER_PORT` / `OUTER_PORT` module constants — `INNER_PORT` is the in-container port the VPS gateway is reached on (distinct from the desktop gateway's `AGENT_SIDE_LATCHKEY_PORT`) and `OUTER_PORT` is the gateway's VPS-loopback bind port.

### Changed

- Changed: **Breaking** — `LatchkeyDiscoveryHandler` now takes an `MngrContext`, and the discovery callback now carries the host id. On discovery, every SSH-reachable agent gets the desktop-side gateway reverse-tunneled onto its `127.0.0.1:AGENT_SIDE_LATCHKEY_PORT` (run inline). Agents whose host also has an accessible outer host (cheap connection-free `outer_host_id_for` check) additionally get the heavy VPS-resident gateway provisioning thrown onto its own fire-and-forget concurrency-group thread, reverse-tunneled onto a distinct `127.0.0.1:INNER_PORT`, so a VPS agent can reach both the desktop and VPS gateways at once.
- Changed: `INNER_PORT` is now `AGENT_SIDE_LATCHKEY_PORT + 1` (not 1989), so the VPS gateway's in-container reverse-tunnel port does not collide with the desktop gateway's in-container port.

## [v0.1.0] - 2026-06-05

### Added

- Added: `LatchkeyForwardSupervisor.bounce()` method that SIGHUPs a live supervisor (or starts one if none is running) so embedders can refresh latchkey's provider set mid-session. `mngr latchkey forward` now refreshes its provider set on SIGHUP instead of shutting down (SIGINT/SIGTERM remain the shutdown signals).
- Added: `libs/mngr_latchkey/scripts/generate_services_json.py`, a developer tool that regenerates the bundled `services.json` permission catalog from a detent checkout's built-in request schemas. Classifies each schema as scope vs. permission (mirroring detent's own doc generator, including the AWS special case) and carries over per-schema `$comment` summaries.
- Added: `mngr latchkey admin-jwt` and `mngr latchkey gateway-info` subcommands; `LatchkeyForwardInfo.gateway_port` stamped for non-spawning consumers.
- Added: New bundled `minds-api-proxy` latchkey gateway extension that reverse-proxies requests under `/minds-api-proxy` to the minds desktop client's bare-origin Minds API, with the upstream URL read at request time from `LATCHKEY_EXTENSION_MINDS_API_URL`; `LatchkeyForwardSupervisor.extra_env` publishes the env var to the detached supervisor on every `minds run` startup.
- Added: `POST /permission-requests/approve/<request_id>` endpoint that merges the pending request's `effect.rules` + `effect.schemas` into the stored `target` permissions.json.
- Added: New `GET /permissions/available` / `GET /permissions/available/<service_name>` catalog endpoints, backed by a `services.json` data file materialized alongside the `.mjs` extensions at gateway-spawn time.
- Added: `register_agent_for_host(plugin_data_dir, host_id, agent_id)` (in `agent_setup.py`) authorizes an agent to reach the Minds API by appending its id to the host permissions file's allowed-agent list — an idempotent, atomic edit that seeds from the baseline when no file exists yet.
- Added: `mngr latchkey register-agent --host-id ID --agent-id ID` CLI wrapping that helper for operators (documented in the README).
- Added: `imbue.mngr_latchkey.store.load_permissions(path)` public reader, symmetric with `save_permissions`.
- Added: Bumped bundled Latchkey to 2.14.0 to support GitHub git operations via the Latchkey gateway.

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
- Changed: `services.json` catalog (and the `permissions` gateway extension that reads it) now maps each raw service name to a *list* of scope entries instead of a single entry, so one service can expose more than one detent scope. `GET /permissions/available` and `GET /permissions/available/<service_name>` now return arrays of `{scope, display_name, permissions}` objects per service.
- Changed: Regenerated `services.json` against the current detent — each scope entry now carries a `description` (detent's `$comment` for the scope), and `permissions` changed from a list of strings to a list of `{"name", "description"}` objects so each permission's summary is colocated with its name. Picks up detent's newer definitions: Slack gains `slack-auth-read`/`slack-auth-write`, and GitLab now exposes a separate `gitlab-git` scope (alongside `gitlab-api`). The `permissions` gateway extension's `GET /permissions/available` and `GET /permissions/available/<service_name>` endpoints surface the scope and per-permission descriptions.
- Changed: `permission_requests` gateway extension now validates the `scope` and `permissions` of incoming `predefined` `POST /permission-requests` bodies against the bundled `services.json` catalog. Requests whose `scope` is not a known Detent scope, or whose `permissions` list contains entries the catalog does not list under that scope, are rejected with HTTP 400 at creation time rather than persisted. File-sharing requests are unaffected.
- Changed: `mngr latchkey forward`'s discovery observer (`mngr observe --discovery-only`) is now the single discovery observer for the host dir and writes to the standard mngr discovery event log. Minds' `mngr forward --observe-via-file` tails the same log instead of running its own observer, removing the multi-observer flicker that earlier required isolating latchkey onto a private per-env event log. Old `discovery-observe/` directories left by prior versions are inert and can be deleted manually.
- Changed: Latchkey forward's discovery consumer now retains agents whose provider errored on a poll rather than tearing down their reverse tunnels, dropping them only on an explicit destroy or a later successful poll.
- Changed: Aligned the workspace's `imbue-mngr*==` pin stragglers in `pyproject.toml` with the satellites bumped in main's release commit, so building the `apps/minds` ToDesktop bundle from main no longer fails at `uv lock`.
- Changed: Added to the release tooling's publish graph (`scripts/utils.py`); will be offered for first publication to PyPI on the next release. Stale `imbue-common==0.1.17` / `concurrency-group==0.1.17` pins in `pyproject.toml` are realigned to the current `0.1.18`. No runtime change.

### Fixed

- Fixed: Race condition in per-directory encryption-key resolution where a concurrent caller could read the on-disk key file mid-write; the file is now published atomically via temp file + `fsync` + `os.link` so the final path only ever exists with complete contents.
- Fixed: `POST /permission-requests/approve/<id>` no longer replaces a symlinked `permissions.json` target with a regular file. The atomic-write helper now `lstat`s the target and resolves symlinks via `realpath` before the temp-file rename, so per-agent symlinks (e.g. those swung in by `mngr latchkey link-permissions`) stay intact and shared canonical permissions remain in sync.
- Fixed: `Latchkey.auth_browser` now transparently recovers from latchkey's "Service `<name>` requires preparation first" error by running `latchkey auth browser-prepare <service>` and retrying `latchkey auth browser <service>` once. Callers (e.g. minds' predefined-permission grant flow) succeed on the first user-visible attempt instead of failing with a confusing error; failures of either step are surfaced as the usual `(False, message)` result.

## [v0.2.8] - 2026-05-13

### Added

- Added: New `imbue-mngr-latchkey` package owning the shared latchkey gateway lifecycle, per-agent wiring, and reverse SSH tunnel; ships as a `mngr` plugin with `mngr latchkey forward` / `create-agent-env` / `link-permissions` subcommands plus a `LatchkeyForwardSupervisor`.

### Changed

- Changed: Latchkey state is now keyed per-host instead of per-agent — `finalize_agent_permissions` → `finalize_host_permissions`, `permissions_path_for_agent` → `permissions_path_for_host`, and `mngr latchkey link-permissions` takes `--host-id`.

### Removed

- Removed: Latchkey on-disk gateway record (`<plugin_data_dir>/latchkey_gateway.json`) and all cross-process gateway adoption helpers — `LatchkeyForwardSupervisor` guarantees one `mngr latchkey forward` per directory.

### Fixed

- Fixed: `mngr latchkey forward` no longer dies with its parent — the parent-death watcher was removed and SIGHUP is wired into the clean coupled-lifetime shutdown path for the interactive case.
