# Changelog - mngr_latchkey

A concise, human-friendly summary of changes for the `mngr_latchkey` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `workspace` permission-request type in the bundled `permission-requests` gateway extension (alongside `predefined` and `file-sharing`) for the cross-workspace `minds-workspaces` API. The extension validates verbs against the `minds-workspaces` set and computes a self-contained grant `effect` applied through the standard approve path; target-scoped verbs accumulate via uniquely-named per-target schemas merged by name. The verb catalog lives in a single shared `extensions/workspace_permissions.json` consumed by both the gateway extension and the Python `workspace_permissions` module.
- Added: `accounts` permission-request type in the same extension. Approval mints a fixed `minds-accounts-read` permission (matching `GET /minds-api-proxy/api/v1/accounts`) under the existing `latchkey-self` scope — no new detent scope, deny-by-default until explicitly granted. All-or-nothing with an empty payload.
- Added: `minds-workspaces` detent permission scope with one named permission per verb (`-read`, `-create`, `-destroy`, `-lifecycle`, `-backups-export`, `-ssh`, `-update`, `-recover`, `-sharing`). Read/create are all-or-nothing; the rest are target-scoped via uniquely-named per-target permission schemas. A verb's `method` may now be a single HTTP method or an array of methods. Added to the per-agent baseline (materialized but not pre-granted) so the user picks which verbs to allow on first cross-workspace call.
- Added: Agent baseline grants `GET /minds-api-proxy/api/schema` by default (a permission on the existing `latchkey-self` scope), so a freshly-created workspace can fetch the OpenAPI description of the Minds API without any user grant.
- Added: `ensure_minds_workspaces_schema_in_existing_host_files` migration backfills the `minds-workspaces` scope + permission schemas into permissions files created before the scope shipped (run at `minds run` startup, before the gateway restarts), plus `store.list_host_permissions_paths` to enumerate the per-host permissions files.
- Added: `cwd` argument on `LatchkeyForwardSupervisor` and the underlying `spawn_detached_mngr_latchkey_forward`, so embedders can launch the detached `mngr latchkey forward` supervisor from a chosen working directory (e.g. `$HOME`) instead of inheriting the caller's cwd.
- Added: Permission-request gateway (`POST /permission-requests`) now validates `agent_id` against the canonical `AgentId` format (`agent-` followed by 32 hex characters) and rejects malformed values with HTTP 400 before persisting. An agent supplying a placeholder like `ENV_AGENT` is now notified at request time rather than silently filing an unusable request that later crashed the desktop client's permission-requests consumer.
- Added: `Latchkey.auth_browser` now owns the full Minds Google OAuth flow — it attempts the browser sign-in optimistically and, only when the service has no registered client yet, prefers the Minds-provided OAuth client (registers it via `auth prepare` and retries against the Minds consent screen) before falling back to the user self-setup flow. The expensive up-front `auth list` probe is gone. The registered Minds client is cleared (`auth clear`) only when we just registered it and its sign-in then failed; a pre-existing client is never cleared.
- Added: Supporting `Latchkey` primitives — `auth_prepare` (register an OAuth client id/secret for a service), `auth_browser_login` (a bare `auth browser` sign-in with no self-setup fallback), and `auth_clear` (`latchkey auth clear -y <service>`). Adds explicit `MINDS_GOOGLE_OAUTH_SERVICES` gate set (`google-directions` is deliberately excluded — it uses an API key, not OAuth) plus the Minds Google OAuth client id/secret as hardcoded constants.

### Changed

- Changed: `services.json` generator preserves manually-curated, non-detent scope entries (like `minds-workspaces`) across regenerations.
- Changed: Bumped pinned latchkey CLI on remote VPS environments (the secondary gateway) to 2.19.1; minimum installed CLI is now 2.19.1.

### Fixed

- Fixed: A granted `minds-workspaces` permission was silently rejected with `403` despite being present in the agent's permissions file. The agent baseline's `latchkey-self` scope is domain-only and matched first, vetoing the request before later rules were consulted. Grant rules are now kept ordered with `latchkey-self` last — both when a grant is approved and when an agent is (re)registered — so a narrower same-domain scope is evaluated first and its grants take effect. Schema and file-sharing access (granted under `latchkey-self` itself) is unaffected.
- Fixed: Repeated macOS system keychain access dialogs (mentioning Latchkey) during normal Minds use. The detached `latchkey ensure-browser` subprocess is now spawned with `LATCHKEY_ENCRYPTION_KEY` injected into its environment, matching other Latchkey invocations.
- Fixed: A SIGHUP provider refresh (sent whenever a workspace toggles a discovery provider, e.g. disabling OVH) could permanently wedge the `mngr latchkey forward` supervisor's discovery pipeline, turning every later SIGHUP bounce into a silent no-op for the rest of that supervisor's life. The `mngr observe` child is now spawned with `is_checked_by_group=False` (its SIGTERM exit on every bounce is expected, not a failure), and the SIGHUP bounce watcher now survives any single bounce's error rather than dying, so later provider toggles still take effect. Shutdown teardown was widened similarly so a slow-to-die observe child can no longer abort the forward's clean shutdown.
- Fixed: Duplicate background discovery producers accumulating from orphan `mngr latchkey forward` processes left by prior or concurrent app instances, which made Minds keep showing errors for providers you had disabled and contributed to a running mind intermittently disappearing (redirecting to "create a mind" with "0 minds"). `LatchkeyForwardSupervisor.ensure_running()` now enforces one forward per latchkey directory: it reaps every `mngr latchkey forward` bound to the same `--latchkey-directory` -- and that forward's `mngr observe` producer, `latchkey gateway`, and reverse `ssh` tunnels -- except the one that matches the live on-disk record. Scoped by resolved `--latchkey-directory` equality, so a supervisor for one profile never signals a sibling profile's forward.

## [v0.1.6] - 2026-06-18

### Added

- Added: `maybe_recover_host_permissions_for_agent` in `agent_setup`: a best-effort repair that, given an agent's opaque permissions handle, host id, and agent id, materializes the canonical per-host permissions file (recreating the opaque handle's symlink if needed) when missing and idempotently re-registers the agent in the host's `minds-api-proxy` allowlist. Cheap when the canonical file already exists.
- Added: `point_opaque_handle_at_host` in `store`: (re)creates an opaque permissions handle as a symlink to the canonical host file without moving anything.

### Changed

- Changed: Adjusted discovery logging to keep log volumes reasonable: `logger.warning()` calls in `discovery.py` are now `logger.opt(exception=e).error(...)` (carrying the underlying exception) or `logger.info(...)` for benign races, and several routine `logger.debug()` lines were downgraded to `logger.trace()`.

### Fixed

- Fixed: Applying a latchkey permission grant could fail with a 500 (`ENOENT ... latchkey_permissions.json.tmp.<hex>`) when the per-host directory did not exist yet. The gateway's `permissions` extension (`POST /permissions/rules`) now creates the target file's parent directories before writing.

## [v0.1.5] - 2026-06-16

### Added

- Added: Exposed the catch-all permission name as a public `WILDCARD_PERMISSION_NAME` constant (still `any`), so consumers like the minds permission dialog can present it as `all` while keeping the stored/granted value unchanged.

## [v0.1.4] - 2026-06-16

### Changed

- Changed: `mngr latchkey forward` now writes a structured, rotated, timestamped JSONL log at `<latchkey_directory>/mngr_latchkey/events.jsonl` — including the shared `latchkey gateway` subprocess output, routed through loguru at DEBUG with a `[latchkey gateway]` prefix — replacing the unrotated `latchkey_gateway.log`. The detached supervisor now spawns with `--quiet` so its raw `latchkey_forward.log` capture stays near-empty in steady state.

## [v0.1.3] - 2026-06-15

## [v0.1.2] - 2026-06-13

### Added

- Added: Notion (MCP) support in the latchkey permission catalog, exposing Notion's hosted MCP endpoint with its grantable permissions.
- Added: File-sharing approvals can now honor a path edited by the user in the approval dialog. The override is re-validated with the same traversal rules used at request creation, and cannot escalate read-only access to read-write.
- Added: `services_catalog` module owns the dialog-facing catalog (`ServicesCatalog` / `ServicePermissionInfo`), previously in the desktop client. It reads bundled `services.json` directly rather than over HTTP, so the gateway's `permissions` extension no longer serves the bare `GET /permissions/available` collection endpoint (the per-service `GET /permissions/available/<service>` endpoint that agents use is unchanged).

### Changed

- Changed: VPS-resident latchkey gateway is now launched with `LATCHKEY_DISABLE_CREDENTIALS_REFRESH=1`, so it no longer races the desktop-side latchkey (the single owner of credential refresh) to rotate the same OAuth refresh token. The remote gateway runs on a synced copy of the user's credentials.
- Changed: Refreshed Slack `slack-read-all` / `slack-write-all` descriptions to match detent's updated wording.
- Changed: VPS-resident latchkey gateway now enforces the same shared `LATCHKEY_GATEWAY_LISTEN_PASSWORD` the local desktop gateway uses (derived from the shared Latchkey encryption key). Previously the remote gateway started without any listen password, so it did not enforce the same authentication.
- Changed: Remote VPS gateways now receive only the latchkey credentials a host's permissions actually grant -- mngr re-encrypts a host-scoped subset via `latchkey auth re-encrypt --services` (encryption key unchanged, so derived passwords and permissions-override JWTs keep validating), limiting the blast radius of a VPS compromise. When nothing is left to ship (deny-all host, or no stored credentials for any granted service) the remote store is cleared instead.
- Changed: Per-agent latchkey gateway setup is decoupled -- a failure to reverse-tunnel the desktop-side gateway into an agent's container no longer prevents the VPS-resident gateway from being provisioned (or vice versa); each reachability path now runs with its own error handling.
- Changed: VPS-resident gateway provisioning is coalesced per outer host: when several agents share one outer host (VPS/container), only one provisioning pass runs at a time instead of multiple agents racing concurrent, redundant passes.
- Changed: Discovery cycle no longer re-provisions an already-provisioned outer host on every emission (the stream re-emits the full agent set continuously, which was flooding logs and the network with redundant SSH work). Each host is provisioned at most once per supervisor lifetime; a failed pass still retries, and a supervisor restart re-provisions. Ongoing credential/permission sync is handled by the remote-state watcher.
- Changed: Replaced a direct RuntimeError raise in the discovery stream consumer with a dedicated `DiscoveryStreamError`.

### Fixed

- Fixed: File-sharing requests are now validated against the Minds WebDAV mount roots (the user's home directory and the system temp directory) at request-creation time and at approve time for a user-edited path override. A grant for any path outside those roots was previously inert (the WebDAV server has no provider for it and answers 404); rejecting it up front gives the agent a clear "must be within a shared root" error instead of an approve-then-404 dead end. Matching mirrors the WebDAV share-prefix matching (case-insensitive, lexical, no symlink resolution or existence check).
- Fixed: File-sharing permission grants for paths with spaces or non-ASCII characters (e.g. `My Documents`) now match incoming requests. The per-file permission pattern is now built from the same WHATWG-URL-normalized (percent-encoded) form the gateway matches incoming requests against, so a path with a space (`%20`) or accented letter actually matches instead of silently never granting access.
- Fixed: File-sharing permission requests accept `~` / `~/...` for the current user's home directory; the gateway expands them to an absolute path before storing the grant. `~user` for another user is rejected with a clear error.
- Fixed: Gateway permissions extension now surfaces the catch-all `any` permission for services whose catalog lists no specific permissions (e.g. Linear). `GET /permissions/available/<service>` injects `any` first; `POST /permission-requests` accepts a `predefined` request naming `any` under any known scope.

### Security

- Security: VPS gateway secrets (encryption key, listen password) are now written to short-lived 0600 random-named files on the VPS that the start script reads into the gateway's environment and deletes immediately, instead of being interpolated into the gateway start command (where they could surface in process listings and command logs). Avoids leaving the encryption key on the VPS disk next to the encrypted credential store it decrypts.

## [v0.1.1] - 2026-06-08

### Added

- Added: Support for running the latchkey gateway on the VPS (the agent's outer host), with the user's credentials and permissions synced from the desktop and the gateway reached over a reverse SSH tunnel. No-op when the outer host is the local machine. Bundles latchkey 2.15.1.
- Added: Remote (VPS) hosts are now kept in sync with the desktop's latchkey state — credential and per-host permission changes are pushed to known remote hosts automatically. Wired into `mngr latchkey forward`.
- Added: A secondary latchkey gateway URL is now injected into tunneled agents' env so a VPS-backed agent can reach the per-VPS gateway. Flows automatically to both `mngr latchkey create-agent-env` and the minds desktop client.
- Added: Distinct in-container and VPS-loopback port constants for the VPS gateway.

### Changed

- Changed: **Breaking** — On discovery, every SSH-reachable agent gets the desktop-side gateway reverse-tunneled to it; agents whose host also has an accessible outer host additionally get the VPS-resident gateway provisioned, so a VPS agent can reach both the desktop and VPS gateways at once.
- Changed: The VPS gateway's in-container reverse-tunnel port no longer collides with the desktop gateway's in-container port.
- Changed: Auto-discovered as a publishable package by the release tooling; will be offered for first publication to PyPI on the next release.

## [v0.1.0] - 2026-06-05

### Added

- Added: `LatchkeyForwardSupervisor.bounce()` method that SIGHUPs a live supervisor (or starts one if none is running) so embedders can refresh latchkey's provider set mid-session. `mngr latchkey forward` now refreshes its provider set on SIGHUP instead of shutting down (SIGINT/SIGTERM remain the shutdown signals).
- Added: A developer tool that regenerates the bundled permission catalog (`services.json`) from a detent checkout's request schemas.
- Added: `mngr latchkey admin-jwt` and `mngr latchkey gateway-info` subcommands; `LatchkeyForwardInfo.gateway_port` stamped for non-spawning consumers.
- Added: New bundled `minds-api-proxy` latchkey gateway extension that reverse-proxies requests under `/minds-api-proxy` to the minds desktop client's Minds API.
- Added: `POST /permission-requests/approve/<request_id>` endpoint that merges a pending request's effect into the stored permissions.
- Added: New `GET /permissions/available` / `GET /permissions/available/<service_name>` catalog endpoints.
- Added: A helper to authorize an agent to reach the Minds API by adding it to the host's allowed-agent list.
- Added: `mngr latchkey register-agent --host-id ID --agent-id ID` CLI wrapping that helper for operators (documented in the README).
- Added: `load_permissions(path)` public reader, symmetric with `save_permissions`.
- Added: Bumped bundled Latchkey to 2.14.0 to support GitHub git operations via the Latchkey gateway.

### Changed

- Changed: Bumped bundled Latchkey to 2.11.1.
- Changed: Switched mngr-latchkey + minds permission management to latchkey 2.9.0's `permission_requests` / `permissions` gateway extensions; `LATCHKEY_MIN_VERSION` bumped to 2.9.0.
- Changed: `permission-requests` extension now uses a typed request schema — `POST /permission-requests` takes `{agent_id, rationale, type, payload}`, where `type` is `"predefined"` or `"file-sharing"`. Pending requests are persisted with new `target` + `effect` fields under `permission_requests/v2/`.
- Changed: File-sharing permission effect now targets the new WebDAV mount, with paths matched transitively (trailing slashes and nested sub-paths) and the full set of WebDAV verbs.
- Changed: File-sharing requests now carry a required `access` field (`READ` / `WRITE`); `READ` unlocks read verbs, `WRITE` additionally unlocks write verbs. `COPY` and `MOVE` are intentionally excluded.
- Changed: Default permissions seeded for every new agent are broadened to let the agent read its own current permissions and the per-service catalog entry.
- Changed: `get_available_services` now returns a typed, pydantic-validated result.
- Changed: The latchkey per-directory encryption key is no longer cached on the long-lived model; it's read (and minted on first use) per subprocess spawn so the secret only lives in memory briefly.
- Changed: The on-disk encryption key file's permission bits are now validated on every load; group/other access raises an error with a `chmod 600` hint.
- Changed: `minds-api-proxy` gateway extension now authenticates forwarded requests to the Minds API on the agent's behalf, so agents never see the API key and cannot spoof one.
- Changed: The agent baseline permissions file now enforces per-agent Minds API isolation, so an agent on one host cannot reach the Minds API on behalf of an agent on another.
- Changed: Latchkey's SSH tunneling now uses the single shared monorepo implementation (`imbue.mngr_forward.ssh_tunnel`).
- Changed: The permission catalog now maps each service to a list of scope entries, so one service can expose more than one detent scope. The `/permissions/available` endpoints return these as arrays.
- Changed: Regenerated the permission catalog against the current detent — each scope and permission now carries a description, and it picks up detent's newer definitions (Slack auth scopes, a separate GitLab `gitlab-git` scope). The `/permissions/available` endpoints surface these descriptions.
- Changed: `predefined` permission requests are now validated against the bundled catalog; an unknown scope or permission is rejected with HTTP 400 at creation time. File-sharing requests are unaffected.
- Changed: `mngr latchkey forward`'s discovery observer is now the single discovery observer for the host dir, writing to the standard mngr discovery event log that minds tails, removing earlier multi-observer flicker. Old `discovery-observe/` directories from prior versions are inert and can be deleted manually.
- Changed: Latchkey forward now retains agents whose provider errored on a poll rather than tearing down their reverse tunnels, dropping them only on explicit destroy or a later successful poll.
- Changed: Aligned `imbue-mngr*` dependency pins with main's release commit, so building the `apps/minds` ToDesktop bundle from main no longer fails at `uv lock`.
- Changed: Added to the release tooling's publish graph; will be offered for first publication to PyPI on the next release. Internal dependency pins realigned to current versions. No runtime change.

### Fixed

- Fixed: Race condition in per-directory encryption-key resolution where a concurrent caller could read the key file mid-write; it's now published atomically.
- Fixed: Approving a permission request no longer replaces a symlinked `permissions.json` with a regular file, so per-agent symlinks (e.g. from `mngr latchkey link-permissions`) stay intact and shared permissions remain in sync.
- Fixed: Browser auth now transparently recovers from latchkey's "requires preparation first" error by preparing the service and retrying once, so callers succeed on the first user-visible attempt instead of failing with a confusing error.

## [v0.2.8] - 2026-05-13

### Added

- Added: New `imbue-mngr-latchkey` package owning the shared latchkey gateway lifecycle, per-agent wiring, and reverse SSH tunnel; ships as a `mngr` plugin with `mngr latchkey forward` / `create-agent-env` / `link-permissions` subcommands plus a `LatchkeyForwardSupervisor`.

### Changed

- Changed: Latchkey state is now keyed per-host instead of per-agent — `finalize_agent_permissions` → `finalize_host_permissions`, `permissions_path_for_agent` → `permissions_path_for_host`, and `mngr latchkey link-permissions` takes `--host-id`.

### Removed

- Removed: Latchkey on-disk gateway record (`<plugin_data_dir>/latchkey_gateway.json`) and all cross-process gateway adoption helpers — `LatchkeyForwardSupervisor` guarantees one `mngr latchkey forward` per directory.

### Fixed

- Fixed: `mngr latchkey forward` no longer dies with its parent — the parent-death watcher was removed and SIGHUP is wired into the clean coupled-lifetime shutdown path for the interactive case.
