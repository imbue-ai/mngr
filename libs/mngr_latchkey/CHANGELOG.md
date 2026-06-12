# Changelog - mngr_latchkey

A concise, human-friendly summary of changes for the `mngr_latchkey` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Notion (MCP) support in the latchkey permission catalog, exposing Notion's hosted MCP endpoint with its grantable permissions.
- Added: File-sharing approvals can now honor a path edited by the user in the approval dialog. The override is re-validated with the same traversal rules used at request creation, and cannot escalate read-only access to read-write.

### Changed

- Changed: VPS-resident latchkey gateway is now launched with `LATCHKEY_DISABLE_CREDENTIALS_REFRESH=1`. The remote gateway runs on a synced copy of the user's credentials, so disabling refresh there prevents it from racing the desktop-side latchkey to rotate the same OAuth refresh token (which would exhaust the user's token and invalidate the desktop's credentials). The desktop-side latchkey remains the single owner of credential refresh.
- Changed: Refreshed Slack `slack-read-all` / `slack-write-all` descriptions to match detent's updated wording.

### Fixed

- Fixed: File-sharing requests are now validated against the Minds WebDAV mount roots (the user's home directory and the system temp directory) at request-creation time and at approve time for a user-edited path override. A grant for any path outside those roots was previously inert (the WebDAV server has no provider for it and answers 404); rejecting it up front gives the agent a clear "must be within a shared root" error instead of an approve-then-404 dead end. Matching mirrors the WebDAV share-prefix matching (case-insensitive, lexical, no symlink resolution or existence check).

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
